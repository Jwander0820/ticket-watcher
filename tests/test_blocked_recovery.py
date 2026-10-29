import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from test_ticketplus import SampleTransport

from ticket_watcher import blocked_recovery
from ticket_watcher.config import QuietWindow
from ticket_watcher.models import SourceError
from ticket_watcher.platforms.ticketplus import TicketPlusAdapter
from ticket_watcher.service import Watcher


def rejection(**diagnostic):
    return SourceError(
        "BLOCKED",
        "拒絕存取",
        http_diagnostic={
            "status": 403,
            "body_read": "complete",
            "body_signals": ["cloudfront", "request_blocked"],
            **diagnostic,
        },
    )


def recovery(w):
    return blocked_recovery.read(w.store.connection)


async def block(h, **diagnostic):
    h["source"].values.append(rejection(**diagnostic))
    result = await h["watcher"].check("test")
    assert result.data["error"]["code"] == "BLOCKED"
    return recovery(h["watcher"])


def failure_notices(w):
    return [
        e
        for e in w.events(detail=True, limit=100).data["events"]
        if e["payload"].get("title") == "⛔ 自動恢復已停止"
    ]


def test_wait_survives_restart_and_only_success_releases_platform(harness):
    h, w = harness, harness["watcher"]

    async def scenario():
        planned = await block(h)
        assert 43200 <= planned["retry_at"] - h["clock"]() <= 86400
        assert w._next_check_at() == planned["retry_at"]
        assert w.health().data["platform_recovery"]["status"] == "WAITING"
        assert w.status().data["platform"]["recovery"]["status"] == "WAITING"
        assert (await w.check("test", immediate=True)).execution_status == "DEFERRED"
        async with Watcher(
            h["config"], client=h["client"], clock=h["clock"], adapter=h["source"]
        ) as restarted:
            restarted.prepare_monitoring()
            assert recovery(restarted) == planned
            h["clock"].now = planned["retry_at"] - 1
            assert await restarted._check_due(asyncio.Event()) == []
            assert h["source"].calls == 1
            h["clock"].advance(1)
            h["source"].push("SOLD_OUT")
            results = await restarted._check_due(asyncio.Event())
            assert len(results) == 1 and results[0].data["complete"]
            assert restarted.store.platform()["paused_reason"] is None
            assert recovery(restarted) is None
            assert h["source"].calls == 2
            assert restarted.query_log.recent()["entries"][0]["mode"] == "recovery"
            assert await restarted._check_due(asyncio.Event()) == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "diagnostic",
    [
        {"status": 401},
        {"cf_mitigated": "challenge"},
        {"aws_waf_action": "captcha"},
        {"aws_waf_action": "challenge"},
        {"body_signals": ["captcha"]},
        {"body_signals": ["cloudflare_challenge"]},
        {"body_signals": ["geo_restriction"]},
        {"body_signals": ["rate_limited"]},
        {"body_read": "timeout"},
        {"body_read": "unavailable_or_too_large"},
        {"body_read": "network_error"},
    ],
)
def test_known_restrictions_and_unreadable_pages_require_manual_resume(harness, diagnostic):
    h, w = harness, harness["watcher"]

    async def scenario():
        assert await block(h, **diagnostic) is None
        h["clock"].advance(2 * 86400)
        assert await w._check_due(asyncio.Event()) == []
        assert h["source"].calls == 1
        assert w.store.platform()["paused_reason"] == "BLOCKED"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["BLOCKED", "NETWORK", "RATE_LIMITED", "PARSE", "partial", "unknown", "missing"]
)
def test_failed_probe_is_terminal_and_notifies_once_even_after_restart(harness, failure):
    h, w = harness, harness["watcher"]

    async def scenario():
        h["source"].push("SOLD_OUT", "SOLD_OUT")
        await w.check("test")
        h["clock"].advance(1000)
        planned = await block(h)
        baseline = w.store.items("test")
        h["clock"].now = planned["retry_at"]
        if failure == "partial":
            h["source"].push("AVAILABLE", "SOLD_OUT", complete=False)
        elif failure == "unknown":
            h["source"].push("UNKNOWN", "SOLD_OUT")
        elif failure == "missing":
            h["source"].push("AVAILABLE")
        else:
            h["source"].values.append(
                rejection() if failure == "BLOCKED" else SourceError(failure, "失敗")
            )
        result = (await w._check_due(asyncio.Event()))[0]
        assert result.execution_status == "FAILED"
        assert recovery(w)["status"] == "FAILED"
        assert w.store.platform()["paused_reason"] == "BLOCKED"
        assert len(failure_notices(w)) == 1
        assert [r["last_valid"] for r in w.store.items("test")] == [
            r["last_valid"] for r in baseline
        ]
        assert not any(e["kind"] == "RELEASE" for e in w.events().data["events"])
        async with Watcher(
            h["config"], client=h["client"], clock=h["clock"], adapter=h["source"]
        ) as other:
            h["clock"].advance(3 * 86400)
            other.prepare_monitoring()
            assert await other._check_due(asyncio.Event()) == []
            assert len(failure_notices(other)) == 1
        assert h["source"].calls == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cancel", "exception", "late_rejection"])
def test_interrupted_or_invalidated_probe_cannot_release_platform(harness, failure):
    h, w = harness, harness["watcher"]

    async def scenario():
        planned = await block(h)
        h["clock"].now = planned["retry_at"]
        entered, finish = asyncio.Event(), asyncio.Event()
        original = h["source"].fetch
        h["source"].push("SOLD_OUT")

        async def fetch(target):
            entered.set()
            await finish.wait()
            if failure == "exception":
                raise RuntimeError("mock error")
            return await original(target)

        h["source"].fetch = fetch
        task = asyncio.create_task(w._check_due(asyncio.Event()))
        await entered.wait()
        assert recovery(w)["status"] == "PROBING"
        assert w.resume(platform=True).data["reason"] == "RECOVERY_IN_PROGRESS"
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if failure == "late_rejection":
                w._error(rejection(cf_mitigated="challenge"), owner="old-owner")
            finish.set()
            if failure == "exception":
                with pytest.raises(RuntimeError):
                    await task
            else:
                results = await task
                assert results[0].execution_status == "DEFERRED"
        assert w.store.platform()["paused_reason"] == "BLOCKED"
        assert recovery(w)["status"] == "FAILED"
        assert len(failure_notices(w)) == 1
        assert w.store.platform()["lease_owner"] is None

    asyncio.run(scenario())


def test_crash_after_claim_never_replays_probe(harness):
    h, w = harness, harness["watcher"]

    async def scenario():
        planned = await block(h)
        h["clock"].now = planned["retry_at"]
        assert (
            w.store.acquire("crashed", h["clock"](), 180, recovery_target=h["config"].targets[0])
            is None
        )
        assert await w._check_due(asyncio.Event()) == []
        h["clock"].advance(181)
        assert await w._check_due(asyncio.Event()) == []
        assert recovery(w)["reason"] == "INTERRUPTED"
        assert h["source"].calls == 1
        assert len(failure_notices(w)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change", ["disabled", "removed", "signature", "route", "expired", "paused"]
)
def test_changed_target_or_route_cancels_pending_probe(harness, change):
    h, w = harness, harness["watcher"]

    async def scenario():
        planned = await block(h)
        target = h["config"].targets[0]
        if change == "removed":
            w.config = replace(w.config, targets=())
        elif change == "route":
            w.config = replace(w.config, ticketplus_proxy="http://another-proxy:40000")
        elif change == "paused":
            w.store.connection.execute("UPDATE targets SET paused_reason='PARSE'")
        else:
            values = {
                "disabled": {"enabled": False},
                "signature": {"item_ids": ("new",)},
                "expired": {"stop_at": planned["retry_at"]},
            }
            w.config = replace(w.config, targets=(replace(target, **values[change]),))
        h["clock"].now = planned["retry_at"]
        assert await w._check_due(asyncio.Event()) == []
        assert recovery(w)["status"] == "FAILED"
        assert w.store.platform()["paused_reason"] == "BLOCKED"
        assert h["source"].calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("gate", ["blocked_until", "lease_until", "next_request", "target"])
def test_probe_respects_existing_deadlines(harness, gate):
    h, w = harness, harness["watcher"]

    async def scenario():
        planned = await block(h)
        later = planned["retry_at"] + 1000
        sql = (
            "UPDATE targets SET next_check=?"
            if gate == "target"
            else f"UPDATE platform SET {gate}=? WHERE id='ticketplus'"
        )
        w.store.connection.execute(sql, (later,))
        h["clock"].now = planned["retry_at"]
        assert w._next_check_at() == later
        assert await w._check_due(asyncio.Event()) == []
        h["clock"].now = later
        h["source"].push("SOLD_OUT")
        assert (await w._check_due(asyncio.Event()))[0].data["complete"]

    asyncio.run(scenario())


def test_service_and_quiet_pause_delay_without_consuming_attempt(harness):
    h, w = harness, harness["watcher"]

    async def scenario():
        planned = await block(h)
        h["clock"].now = planned["retry_at"]
        w.config = replace(w.config, ui_paused=True)
        assert await w._check_due(asyncio.Event()) == []
        assert recovery(w) == planned
        # The clock is UTC for this test; the pause ends at the next hour.
        dt = datetime.fromtimestamp(h["clock"](), UTC)
        start, end = dt.hour * 60, ((dt.hour + 1) % 24) * 60
        w.config = replace(
            w.config,
            ui_paused=False,
            timezone="UTC",
            quiet_windows=(QuietWindow(start, end, None),),
        )
        assert await w._check_due(asyncio.Event()) == []
        assert recovery(w)["status"] == "WAITING"
        assert recovery(w)["retry_at"] > planned["retry_at"]
        assert h["source"].calls == 1

    asyncio.run(scenario())


def test_manual_resume_discards_pending_attempt_and_old_pause_is_not_inferred(harness):
    h, w = harness, harness["watcher"]

    async def scenario():
        await block(h)
        w.resume(platform=True)
        assert recovery(w) is None
        w.store.connection.execute(
            "UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'"
        )
        h["clock"].advance(3 * 86400)
        assert await w._check_due(asyncio.Event()) == []
        assert h["source"].calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,headers",
    [
        (200, {"cf-mitigated": "challenge"}),
        (202, {"x-amzn-waf-action": "challenge"}),
        (405, {"x-amzn-waf-action": "captcha"}),
    ],
)
def test_challenge_headers_pause_without_recovery_even_without_body_markers(
    harness, status, headers
):
    h, w = harness, harness["watcher"]

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(status, json={}, headers=headers)
            )
        ) as client:
            w.transport.client = client
            w.transport.owner = "test-owner"
            w.store.acquire("test-owner", h["clock"](), 180)
            with pytest.raises(SourceError) as caught:
                await w.transport.get_json("https://apis.ticketplus.com.tw/config/api/v1/get", {})
            assert caught.value.http_diagnostic["status"] == status
            w.store.sync_target(h["config"].targets[0])
            w._error(caught.value, h["config"].targets[0], owner="test-owner")
            assert w.store.platform()["paused_reason"] == "BLOCKED"
            assert recovery(w) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("expire_metadata", [False, True])
def test_real_adapter_probe_keeps_other_workers_and_targets_paused(harness, expire_metadata):
    h, w = harness, harness["watcher"]
    fixture = SampleTransport(h)
    target = h["config"].targets[0]
    w.config = replace(w.config, request_gap=(0, 0), targets=(target, replace(target, id="second")))
    w.transport.config = w.config
    w.adapter = TicketPlusAdapter(w.transport)
    phase, requests = ["baseline"], []

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def respond(request):
            requests.append(request)
            if phase[0] == "blocked":
                return httpx.Response(403, text="Request blocked. Generated by CloudFront.")
            if phase[0] == "probe":
                assert w.store.platform()["paused_reason"] == "BLOCKED"
                entered.set()
                await finish.wait()
            return httpx.Response(
                200, json=await fixture.get_json(str(request.url), dict(request.url.params))
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            w.transport.client = client
            assert (await w.check("test")).data["complete"]
            w.store.sync_target(w.config.targets[1])
            phase[0] = "blocked"
            h["clock"].advance(1000)
            assert (await w.check("test")).execution_status == "FAILED"
            planned = recovery(w)
            h["clock"].now = planned["retry_at"]
            if expire_metadata:
                w.transport.config = replace(w.config, metadata_cache_seconds=1)
            phase[0] = "probe"
            before = len(requests)
            async with Watcher(w.config, client=client, clock=h["clock"]) as other:
                task = asyncio.create_task(w._check_due(asyncio.Event()))
                await asyncio.wait_for(entered.wait(), 1)
                assert await other._check_due(asyncio.Event()) == []
                assert (await other.check("second", immediate=True)).data[
                    "reason"
                ] == "PLATFORM_PAUSED"
                assert (await other.query(target.url)).data["reason"] == "PLATFORM_PAUSED"
                assert (await other.refresh_metadata("test")).data["reason"] == "PLATFORM_PAUSED"
                finish.set()
                results = await task
                assert len(results) == 1 and results[0].data["complete"]
                assert len(requests) - before == (3 if expire_metadata else 1)
                assert other.store.platform()["paused_reason"] is None
                assert other.store.target("second")["next_check"] > h["clock"]()
                assert await other._check_due(asyncio.Event()) == []

    asyncio.run(scenario())


def test_late_cancelled_rejection_invalidates_pending_probe(harness):
    from test_vps_optimizations import Chunks

    h, w = harness, harness["watcher"]

    async def scenario():
        await block(h)
        stream = Chunks([b"partial"], stall=True)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(403, stream=stream))
        ) as client:
            w.transport.client = client
            task = asyncio.create_task(
                w.transport._request("https://apis.ticketplus.com.tw/config/api/v1/get", {})
            )
            async with asyncio.timeout(1):
                while not stream.reads:
                    await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert recovery(w)["invalidated"]
            assert w._next_check_at() == h["clock"]()
            assert await w._check_due(asyncio.Event()) == []
            assert recovery(w)["reason"] == "LATE_REJECTION"
            assert len(failure_notices(w)) == 1

    asyncio.run(scenario())


def test_failed_probe_notification_uses_target_channels_and_existing_alert_switch(harness):
    h, w = harness, harness["watcher"]
    target = replace(w.config.targets[0], channel_ids=("one", "two"))
    w.config = replace(w.config, system_alerts=True, targets=(target,))

    async def scenario():
        planned = await block(h)
        h["clock"].now = planned["retry_at"]
        h["source"].values.append(rejection())
        await w._check_due(asyncio.Event())
        event = failure_notices(w)[0]
        rows = w.store.connection.execute(
            "SELECT channel_id,status FROM outbox WHERE event_id=?", (event["id"],)
        ).fetchall()
        assert {tuple(r) for r in rows} == {("one", "PENDING"), ("two", "PENDING")}

    asyncio.run(scenario())


def test_ui_exposes_pending_recovery_and_manual_resume_cancels_it(tmp_path):
    from conftest import URL, Source
    from test_web import mutate, panel, snapshot

    async def scenario():
        async with panel(tmp_path) as (client, controller, requests):
            response = await mutate(
                client,
                await snapshot(client),
                "/api/targets",
                {
                    "name": "恢復測試",
                    "url": URL,
                    "enabled": True,
                },
            )
            state = await response.json()
            ident = state["targets"][0]["id"]
            source = Source(controller.watcher.clock)
            source.values.append(rejection())
            controller.watcher.adapter = source
            # Action bodies use target_id directly rather than a settings value.
            response = await client.post(
                "/api/actions/check",
                headers={"X-CSRF-Token": state["csrf"]},
                json={"revision": state["revision"], "target_id": ident},
            )
            assert (await response.json())["execution_status"] == "FAILED"
            state = await snapshot(client)
            info = state["health"]["platform_recovery"]
            assert info["status"] == "WAITING" and info["retry_at"]
            assert "owner" not in info and "route_key" not in info and "signature" not in info
            assert state["query_logs"]["entries"][0]["next_at"] == info["retry_at"]
            response = await client.post(
                "/api/actions/resume",
                headers={"X-CSRF-Token": state["csrf"]},
                json={"revision": state["revision"], "platform": True},
            )
            assert (await response.json())["execution_status"] == "COMPLETED"
            updated = await snapshot(client)
            assert updated["health"]["platform_recovery"] is None
            assert updated["health"]["platform_paused"] is None
            assert not requests

    asyncio.run(scenario())
