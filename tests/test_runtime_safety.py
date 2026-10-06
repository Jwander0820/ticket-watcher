import asyncio
import copy
from dataclasses import replace

import httpx
import pytest
from conftest import URL, Source
from test_web import mutate, panel, snapshot

import ticket_watcher.web as web_module
from ticket_watcher.models import SourceError
from ticket_watcher.service import Watcher
from ticket_watcher.web import Controller


@pytest.mark.parametrize("restriction", ["RATE_LIMITED", "BLOCKED"])
@pytest.mark.parametrize("operation", ["query", "check"])
def test_late_platform_restriction_stops_current_query_before_next_http(
    harness, restriction, operation
):
    h, w = harness, harness["watcher"]
    requests = []

    async def scenario():
        h["source"].push("SOLD_OUT")
        await w._check("test", immediate=True)
        baseline = w.store.items("test")
        state = w.store.target("test")
        w.store.acquire("old-query", h["clock"](), 180)
        h["clock"].advance(181)

        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                result = w._error(SourceError(restriction, "late response", 900), owner="old-query")
                assert result.data["reason"] == "QUERY_SUPERSEDED"
            return httpx.Response(200, json={"ok": True})

        class TwoRequestSource:
            async def fetch(self, target):
                await w.transport.get_json("https://example.invalid/first", {})
                h["clock"].advance(w.config.request_gap)
                await w.transport.get_json("https://example.invalid/second", {})
                h["source"].push("AVAILABLE")
                return await h["source"].fetch(target)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.transport.client = client
            w.adapter = TwoRequestSource()
            result = (
                await w.query(URL)
                if operation == "query"
                else await w._check("test", immediate=True)
            )
        assert len(requests) == 1
        assert result.execution_status == "DEFERRED"
        assert result.data["reason"] == (
            "PLATFORM_PAUSED" if restriction == "BLOCKED" else "PLATFORM_BUSY_OR_BACKOFF"
        )
        platform = w.store.platform()
        assert platform["lease_owner"] is None and platform["failures"] == 0
        if restriction == "BLOCKED":
            assert platform["paused_reason"] == "BLOCKED"
        else:
            assert platform["blocked_until"] > h["clock"]()
            assert result.data["next_allowed_at"] is not None
        assert w.store.items("test") == baseline
        assert w.store.target("test") == state
        assert not w.events().data["events"]

    asyncio.run(scenario())


@pytest.mark.parametrize("late_error", [None, "NETWORK", "BLOCKED", "RATE_LIMITED"])
def test_superseded_check_cannot_change_newer_baseline_or_errors(harness, late_error):
    h, w = harness, harness["watcher"]

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        source = Source(h["clock"])
        source.push("AVAILABLE")

        class SlowSource:
            async def fetch(self, target):
                started.set()
                await finish.wait()
                if late_error:
                    raise SourceError(late_error, "late error", 500)
                return await source.fetch(target)

        w.adapter = SlowSource()
        pending = asyncio.create_task(w._check("test", immediate=True))
        await started.wait()
        h["clock"].advance(w.transport.lease_seconds + 1)
        h["source"].push("SOLD_OUT")
        async with Watcher(
            h["config"], client=h["client"], clock=h["clock"], adapter=h["source"]
        ) as other:
            assert (await other._check("test", immediate=True)).execution_status == "COMPLETED"
            baseline = other.store.items("test")
            schedule = other.store.target("test")
            platform = other.store.platform()
            finish.set()
            stale = await pending
            assert stale.execution_status == "DEFERRED"
            assert stale.data["reason"] == "QUERY_SUPERSEDED"
            assert other.store.items("test") == baseline
            assert other.store.target("test") == schedule
            if late_error == "RATE_LIMITED":
                platform["blocked_until"] = h["clock"]() + h["config"].backoff[0]
            elif late_error == "BLOCKED":
                platform["paused_reason"] = "BLOCKED"
            assert other.store.platform() == platform
            assert not other.events().data["events"]

    asyncio.run(scenario())


def test_same_watcher_keeps_http_query_owners_separate(harness):
    h, w = harness, harness["watcher"]

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handle(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await finish.wait()
            return httpx.Response(200, json={"value": calls})

        class HttpSource:
            async def fetch(self, target):
                await w.transport.get_json("https://example.invalid/status", {})
                h["source"].push("SOLD_OUT")
                return await h["source"].fetch(target)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.transport.client = client
            w.adapter = HttpSource()
            pending = asyncio.create_task(w._check("test", immediate=True))
            await started.wait()
            h["clock"].advance(w.transport.lease_seconds + 1)
            result = await w._check("test", immediate=True)
            assert result.execution_status == "COMPLETED"
            finish.set()
            assert (await pending).data["reason"] == "QUERY_SUPERSEDED"
            assert h["source"].calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [None, "NETWORK"])
def test_superseded_query_does_not_reset_or_extend_platform_backoff(harness, error):
    h, w = harness, harness["watcher"]

    class LateSource:
        async def fetch(self, target):
            h["clock"].advance(w.transport.lease_seconds + 1)
            w.store.acquire("replacement", h["clock"](), 180)
            w.store.connection.execute(
                "UPDATE platform SET failures=2,blocked_until=? WHERE id='ticketplus'",
                (h["clock"]() + 900,),
            )
            if error:
                raise SourceError(error, "late response")
            h["source"].push("AVAILABLE")
            return await h["source"].fetch(target)

    w.adapter = LateSource()
    assert asyncio.run(w.query(URL)).data["reason"] == "QUERY_SUPERSEDED"
    assert w.store.platform()["failures"] == 2
    assert w.store.platform()["lease_owner"] == "replacement"
    assert w.store.platform()["blocked_until"] == h["clock"]() + 900


def test_ticket_http_request_has_total_timeout(harness):
    h, w = harness, harness["watcher"]
    w.transport.config = replace(h["config"], timeout=0.01)

    async def scenario():
        async def handle(request):
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.transport.client = client
            w.transport.owner = "bounded"
            w.store.acquire("bounded", h["clock"](), 180)
            with pytest.raises(SourceError) as error:
                await asyncio.wait_for(w.transport.get_json("https://example.invalid/", {}), 1)
            assert error.value.code == "NETWORK"

    asyncio.run(scenario())


def test_save_constructor_failure_keeps_ui_readable_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(web_module, "WORKER_RETRY_SECONDS", (0.02,))
    monkeypatch.setattr(web_module, "WORKER_STABLE_SECONDS", 0.02)
    path = tmp_path / "ui-config.yaml"
    path.write_text(
        "app:\n  database_path: state.db\nnotifications:\n"
        "  webhook_url_env: REVIEW_UNSET\ntargets: []\n"
    )

    async def scenario():
        instances = []
        calls = 0
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:

            def factory(config):
                nonlocal calls
                calls += 1
                if calls in (2, 3):
                    raise OSError("transient startup failure")
                watcher = Watcher(config, client=client)
                instances.append(watcher)
                return watcher

            c = Controller(path, watcher_factory=factory)
            await c.start()
            try:
                document = copy.deepcopy(c.document)
                document["polling"] = {"normal_interval_seconds": [400, 900]}
                async with c.lock:
                    await c.save(document, {})
                    state = c.state()
                assert state["runner_active"] and state["runner_error"]
                assert not state["health"]["process_healthy"]
                assert state["settings"]["polling"]["normal_interval_seconds"] == [400, 900]
                async with asyncio.timeout(2):
                    while c.watcher is None or c.runner_error:
                        await asyncio.sleep(0.005)
                assert calls == 4 and c.watcher.config.normal_interval == (400, 900)
                assert len(c.watcher.events().data["events"]) == 2
            finally:
                await c.close()

    asyncio.run(scenario())


def test_manual_check_leaves_state_readable_and_reload_cancels_safely(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            response = await mutate(
                client,
                await snapshot(client),
                "/api/targets",
                {
                    "name": "test",
                    "url": URL,
                    "enabled": True,
                },
            )
            state = await response.json()
            started = asyncio.Event()

            class SlowSource:
                async def fetch(self, target):
                    started.set()
                    await asyncio.Event().wait()

            old = c.watcher
            old.adapter = SlowSource()
            pending = asyncio.create_task(
                client.post(
                    "/api/actions/check",
                    headers={"X-CSRF-Token": state["csrf"]},
                    json={"revision": state["revision"], "target_id": state["targets"][0]["id"]},
                )
            )
            await started.wait()
            updated = await asyncio.wait_for(snapshot(client), 1)
            assert updated["targets"]
            response = await asyncio.wait_for(
                mutate(
                    client,
                    updated,
                    "/api/settings",
                    {
                        "polling": {"normal_interval_seconds": [400, 900]},
                    },
                ),
                1,
            )
            assert response.status == 200
            assert (await pending).status == 409
            assert c.watcher is not old
            assert not c.actions and not requests

    asyncio.run(scenario())


def test_manual_check_does_not_wait_for_discord(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            response = await mutate(
                client,
                await snapshot(client),
                "/api/targets",
                {
                    "name": "test",
                    "url": URL,
                    "enabled": True,
                },
            )
            state = await response.json()
            source = Source(c.watcher.clock)
            source.push("SOLD_OUT")
            c.watcher.adapter = source

            async def forbidden_delivery():
                raise AssertionError("manual check must leave delivery to the worker")

            c.watcher.notifier.deliver = forbidden_delivery
            response = await client.post(
                "/api/actions/check",
                headers={"X-CSRF-Token": state["csrf"]},
                json={"revision": state["revision"], "target_id": state["targets"][0]["id"]},
            )
            assert response.status == 200
            assert (await response.json())["execution_status"] == "COMPLETED"

    asyncio.run(scenario())
