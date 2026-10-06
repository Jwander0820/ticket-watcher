import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from conftest import URL
from test_monitoring import check, releases
from test_notifications import enable

from ticket_watcher import service
from ticket_watcher.models import SourceError
from ticket_watcher.service import Watcher
from ticket_watcher.storage import Store


@pytest.mark.parametrize("change", ["config", "sold_out"])
def test_inflight_failure_cannot_revive_cancelled_release(harness, monkeypatch, change):
    h = harness
    w = h["watcher"]
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    event_id = releases(w)[0]["id"]
    enable(h, monkeypatch)
    target = h["config"].targets[0]
    if change == "config":
        target = replace(target, session_ids=("s000000001",))
    config = replace(h["config"], targets=(target,))
    other = Watcher(config, client=h["client"], clock=h["clock"], adapter=h["source"])

    async def handle(request):
        if change == "config":
            other.store.sync_target(target)
        else:
            h["source"].push("SOLD_OUT")
            other._apply(target, await h["source"].fetch(target))
        h["source"].push("AVAILABLE")
        other._apply(target, await h["source"].fetch(target))
        assert other.store.notification_status(event_id)["status"] == "CANCELLED"
        return httpx.Response(500)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.notifier.client = client
            await w.notifier.deliver(max_messages=1)
        assert w.store.notification_status(event_id)["status"] == "CANCELLED"
        h["clock"].advance(10)
        await other.notifier.deliver()
        messages = [json.loads(request.content)["content"] for request in h["requests"]]
        assert all(event_id not in message for message in messages)
        assert len(messages) == (1 if change == "sold_out" else 0)

    try:
        asyncio.run(scenario())
    finally:
        other.store.close()


@pytest.mark.parametrize("status", ["SENT", "PENDING"])
def test_old_sender_cannot_complete_or_unlock_reclaimed_notice(harness, status):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    store = h["watcher"].store
    other = Store(h["config"].database_path)
    try:
        old = store.claim_notice(h["clock"]())
        h["clock"].advance(121)
        current = other.claim_notice(h["clock"]())
        assert current["claim_token"] != old["claim_token"]
        assert not store.finish_notice(old, h["clock"](), status, message_id="old")
        assert other.platform("discord")["lease_owner"] == current["claim_token"]
        assert other.notification_status(current["event_id"])["status"] == "INFLIGHT"
        assert other.finish_notice(current, h["clock"](), "SENT", message_id="new")
        assert store.notification_status(old["event_id"])["message_id"] == "new"
    finally:
        other.close()


def test_event_signature_prevents_delivery_against_different_baseline(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    event = releases(h["watcher"])[0]
    assert event["payload"]["target_signature"] == h["config"].targets[0].signature
    # An old pending event must also be rejected if its cancellation was missed.
    target = replace(h["config"].targets[0], session_ids=("s000000001",))
    h["watcher"].store.connection.execute(
        "UPDATE targets SET signature=? WHERE id='test'", (target.signature,)
    )
    h["watcher"].notifier.config = replace(h["config"], targets=(target,))
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert h["watcher"].store.notification_status(event["id"])["status"] == "CANCELLED"
    assert not h["requests"]


def test_legacy_pending_event_without_signature_can_still_be_delivered(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    event = releases(h["watcher"])[0]
    event["payload"].pop("target_signature")
    h["watcher"].store.connection.execute(
        "UPDATE events SET payload=? WHERE id=?", (json.dumps(event["payload"]), event["id"])
    )
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert h["watcher"].store.notification_status(event["id"])["status"] == "SENT"


def test_cancelled_inflight_notice_still_preserves_discord_rate_limit(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    w = h["watcher"]
    event_id = releases(w)[0]["id"]
    enable(h, monkeypatch)

    async def handle(request):
        with w.store.transaction() as db:
            w.store.cancel_unavailable(db, "test", {"s000000001"})
        return httpx.Response(429, json={"retry_after": 180})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.notifier.client = client
            await w.notifier.deliver()

    asyncio.run(scenario())
    assert w.store.notification_status(event_id)["status"] == "CANCELLED"
    assert w.store.platform("discord")["blocked_until"] == h["clock"]() + 180


def test_notification_total_timeout_releases_claim_for_retry(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    w = h["watcher"]
    event_id = releases(w)[0]["id"]
    enable(h, monkeypatch)
    w.notifier.config = replace(h["config"], timeout=0.01)

    async def handle(request):
        await asyncio.Event().wait()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.notifier.client = client
            await asyncio.wait_for(w.notifier.deliver(max_messages=1), 1)

    asyncio.run(scenario())
    notice = w.store.notification_status(event_id)
    assert notice["status"] == "PENDING" and notice["last_error"] == "NETWORK_OR_TIMEOUT"
    assert w.store.platform("discord")["lease_owner"] is None


def test_query_success_resets_backoff_without_changing_cooldown_or_baseline(harness):
    h = harness
    w = h["watcher"]
    check(h, "SOLD_OUT")
    baseline = w.store.items("test")
    h["source"].values.append(SourceError("NETWORK", "mock timeout"))
    asyncio.run(w.query(URL))
    wait = w.store.platform()["blocked_until"]
    h["clock"].now = wait
    h["source"].push("AVAILABLE")
    asyncio.run(w.query(URL))
    assert w.store.platform()["failures"] == 0
    assert w.store.platform()["blocked_until"] == wait
    assert w.store.items("test") == baseline
    assert not w.events().data["events"]
    h["source"].values.append(SourceError("NETWORK", "mock timeout"))
    asyncio.run(w.query(URL))
    assert 900 <= w.store.platform()["blocked_until"] - h["clock"]() <= 930


def test_partial_query_does_not_reset_failure_counter(harness):
    h = harness
    w = h["watcher"]
    h["source"].values.append(SourceError("NETWORK", "mock timeout"))
    asyncio.run(w.query(URL))
    h["clock"].now = w.store.platform()["blocked_until"]
    h["source"].push("UNKNOWN", complete=False)
    asyncio.run(w.query(URL))
    assert w.store.platform()["failures"] == 1


def enqueue_system(watcher, now):
    with watcher.store.transaction() as db:
        watcher.store.enqueue(
            db, "system-test", None, "SYSTEM", now, {"message": "mock"}, 600, True
        )


def test_tick_checks_next_target_while_notification_is_inflight(harness, monkeypatch):
    h = harness
    w = h["watcher"]
    target = replace(h["config"].targets[0], id="second")
    w.config = replace(h["config"], targets=(*h["config"].targets, target))
    w.notifier.config = w.config
    enable(h, monkeypatch)
    enqueue_system(w, h["clock"]())

    async def scenario():
        started, release, visited = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handle(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"id": "123"})

        original = h["source"].fetch

        async def fetch(target):
            if target.id == "second":
                await started.wait()
                assert not release.is_set()
                visited.set()
            h["source"].push("SOLD_OUT")
            return await original(target)

        monkeypatch.setattr(h["source"], "fetch", fetch)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.notifier.client = client
            task = asyncio.create_task(w.tick())
            try:
                await asyncio.wait_for(visited.wait(), 1)
            finally:
                release.set()
                result = await asyncio.wait_for(task, 1)
        assert len(result.data["checks"]) == 2
        assert result.data["delivery"]["sent"] == 1

    asyncio.run(scenario())


def test_run_keeps_polling_and_cleans_up_tasks_during_slow_delivery(harness, monkeypatch):
    h = harness
    w = h["watcher"]
    enable(h, monkeypatch)
    enqueue_system(w, h["clock"]())

    async def scenario():
        started, second, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handle(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        original = h["source"].fetch

        async def fetch(target):
            h["source"].push("SOLD_OUT")
            observation = await original(target)
            if h["source"].calls == 2:
                second.set()
            return observation

        monkeypatch.setattr(h["source"], "fetch", fetch)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.notifier.client = client
            task = asyncio.create_task(w.run())
            try:
                await asyncio.wait_for(started.wait(), 1)
                h["clock"].advance(300)
                w.wake()  # Advance the fake clock without waiting 300 real seconds.
                await asyncio.wait_for(second.wait(), 1)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert cancelled.is_set()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(scenario())


def test_heartbeat_updates_during_long_query(harness, monkeypatch):
    h = harness
    w = h["watcher"]
    monkeypatch.setattr(service, "HEARTBEAT_INTERVAL_SECONDS", 0.01)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = h["source"].fetch

        async def fetch(target):
            entered.set()
            await release.wait()
            h["source"].push("SOLD_OUT")
            return await original(target)

        monkeypatch.setattr(h["source"], "fetch", fetch)
        task = asyncio.create_task(w.tick())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            h["clock"].advance(121)

            async def updated():
                while not w.health().data["process_healthy"]:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(updated(), 1)
            assert not task.done()
        finally:
            release.set()
            await asyncio.wait_for(task, 1)
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(scenario())


def test_heartbeat_continues_while_tick_waits_for_final_delivery(harness, monkeypatch):
    h = harness
    w = h["watcher"]
    w.notifier.config = replace(h["config"], timeout=180)
    enable(h, monkeypatch)
    enqueue_system(w, h["clock"]())
    monkeypatch.setattr(service, "HEARTBEAT_INTERVAL_SECONDS", 0.01)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handle(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"id": "123"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            w.notifier.client = client
            h["source"].push("SOLD_OUT")
            task = asyncio.create_task(w.tick())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                h["clock"].advance(121)

                async def updated():
                    while not w.health().data["process_healthy"]:
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(updated(), 1)
                assert h["source"].calls == 1 and not task.done()
            finally:
                release.set()
                await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


def test_status_paginates_before_decoding_and_keeps_full_summary(harness, monkeypatch):
    h = harness
    check(h, *("SOLD_OUT" for _ in range(60)), *("AVAILABLE" for _ in range(20)))
    w = h["watcher"]
    decoded = []
    original = json.loads

    def loads(value):
        decoded.append(value)
        return original(value)

    def no_full_scan(*args):
        raise AssertionError("status must not load every item's details")

    monkeypatch.setattr(w.store, "items", no_full_scan)
    monkeypatch.setattr(service.json, "loads", loads)
    summary = w.status("test").data["targets"][0]
    assert summary["summary"] == {"SOLD_OUT": 60, "AVAILABLE": 20}
    assert not decoded
    result = w.status("test", detail=True, limit=2, offset=60).data["targets"][0]
    assert result["summary"] == summary["summary"]
    assert result["current_observation"] == summary["summary"]
    assert len(decoded) == 2
    assert result["page"]["total"] == 80 and result["page"]["next_offset"] == 62
    assert [x["item_key"] for x in result["page"]["items"]] == ["s000000061", "s000000062"]
    assert w.status("test", detail=True, offset=80).data["targets"][0]["page"] == {
        "items": [],
        "total": 80,
        "next_offset": None,
    }


def test_pruning_is_hourly_across_processes_and_preserves_pending_work(harness):
    h = harness
    store = h["watcher"].store
    now = h["clock"]()
    store.prune(now, 30)
    with store.transaction() as db:
        db.execute("INSERT INTO events VALUES('old',NULL,'ERROR',?,'{}')", (now - 31 * 86400,))
        store.enqueue(
            db, "pending", None, "SYSTEM", now - 31 * 86400, {"message": "mock"}, 600, True
        )
    other = Store(h["config"].database_path)
    try:
        other.prune(now + 3599, 30)
        assert store.event_page(None, 10, 0)["total"] == 2
        other.prune(now + 3600, 30)
        assert [x["id"] for x in store.event_page(None, 10, 0)["events"]] == ["pending"]
        assert store.notification_status("pending")["status"] == "PENDING"
    finally:
        other.close()
