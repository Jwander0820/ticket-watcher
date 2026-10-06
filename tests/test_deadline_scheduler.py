import asyncio
from dataclasses import replace

import httpx
import pytest
from test_notifications import enable

from ticket_watcher import service
from ticket_watcher.storage import Store


async def eventually(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


async def cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def enqueue(w, event_id="test-notice", *, ttl=600):
    with w.store.transaction() as db:
        w.store.enqueue(db, event_id, None, "SYSTEM", w.clock(), {"message": "mock"}, ttl, True)


def test_idle_run_waits_for_deadline_without_repeated_scans(harness, monkeypatch):
    h, w = harness, harness["watcher"]
    counts = {"poll": 0, "delivery": 0, "heartbeat": 0, "rotation": 0}
    deadlines = []
    monkeypatch.setattr(service, "HEARTBEAT_INTERVAL_SECONDS", 0.005)

    async def scenario():
        h["source"].push("SOLD_OUT")
        await w._check("test", immediate=True)
        poll, deliver, heartbeat, maintain, wait = (
            w._check_due,
            w.notifier.deliver,
            w.store.heartbeat,
            w.query_log.maintain,
            w._wait_until,
        )

        async def check_due(wakeup):
            counts["poll"] += 1
            return await poll(wakeup)

        async def delivery():
            counts["delivery"] += 1
            return await deliver()

        def beat(now):
            counts["heartbeat"] += 1
            heartbeat(now)

        def rotation():
            counts["rotation"] += 1
            maintain()

        async def waiting(changed, deadline):
            if changed in w._poll_waiters:
                deadlines.append(deadline)
            await wait(changed, deadline)

        monkeypatch.setattr(w, "_check_due", check_due)
        monkeypatch.setattr(w.notifier, "deliver", delivery)
        monkeypatch.setattr(w.store, "heartbeat", beat)
        monkeypatch.setattr(w.query_log, "maintain", rotation)
        monkeypatch.setattr(w, "_wait_until", waiting)
        task = asyncio.create_task(w.run())
        try:
            await eventually(lambda: counts["heartbeat"] >= 4)
            assert counts["poll"] == counts["delivery"] == counts["rotation"] == 1
            assert deadlines == [h["clock"]() + 300]
            assert h["source"].calls == 1
        finally:
            await cancel(task)
        assert not w._poll_waiters and not w._delivery_waiters

    asyncio.run(scenario())


@pytest.mark.parametrize("restriction", ["none", "cooldown", "lease", "paused", "stop"])
def test_poll_waits_for_earliest_eligible_work(harness, monkeypatch, restriction):
    h, w = harness, harness["watcher"]
    now = h["clock"]()
    first = h["config"].targets[0]
    second = replace(first, id="second")
    if restriction == "stop":
        first = replace(first, stop_at=now + 40)
    w.config = replace(w.config, targets=(first, second))
    w.store.sync_target(first)
    w.store.sync_target(second)
    w.store.connection.execute("UPDATE targets SET next_check=? WHERE id='test'", (now + 300,))
    w.store.connection.execute("UPDATE targets SET next_check=? WHERE id='second'", (now + 60,))
    if restriction == "cooldown":
        w.store.connection.execute(
            "UPDATE platform SET blocked_until=? WHERE id='ticketplus'", (now + 900,)
        )
    elif restriction == "lease":
        w.store.acquire("external", now, 180)
    elif restriction == "paused":
        w.store.connection.execute(
            "UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'"
        )
    expected = {
        "none": now + 60,
        "cooldown": now + 900,
        "lease": now + 180,
        "paused": None,
        "stop": now + 40,
    }[restriction]

    async def scenario():
        waiting = asyncio.Event()

        async def wait(changed, deadline):
            assert deadline == expected
            waiting.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(w, "_wait_until", wait)
        task = asyncio.create_task(w._poll_loop(asyncio.Event()))
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            assert h["source"].calls == 0
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_manual_check_wakes_idle_delivery_immediately(harness):
    h, w = harness, harness["watcher"]
    # Use fixture-controlled webhook configuration without touching real credentials.
    from unittest.mock import patch

    async def scenario():
        h["source"].push("SOLD_OUT")
        await w._check("test", immediate=True)
        task = asyncio.create_task(w.run())
        try:
            await eventually(lambda: bool(w._delivery_waiters) and bool(w._poll_waiters))
            h["source"].push("AVAILABLE")
            result = await w._check("test", immediate=True, mode="manual")
            await eventually(
                lambda: w.store.notification_status(result.data["event_id"])["status"] == "SENT"
            )
            assert len(h["requests"]) == 1 and h["source"].calls == 2
        finally:
            await cancel(task)

    with patch.dict(
        "os.environ",
        {"TICKET_WATCHER_TEST_WEBHOOK": "https://discord.com/api/webhooks/123/test_only"},
    ):
        asyncio.run(scenario())


@pytest.mark.parametrize("action", ["resume", "enqueue"])
def test_external_process_changes_wake_sleeping_worker(harness, monkeypatch, action):
    h, w = harness, harness["watcher"]
    monkeypatch.setattr(service, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
    enable(h, monkeypatch)
    w.store.connection.execute("UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'")

    async def scenario():
        other = Store(h["config"].database_path)
        task = asyncio.create_task(w.run())
        try:
            await eventually(lambda: bool(w._poll_waiters) and bool(w._delivery_waiters))
            if action == "resume":
                h["source"].push("SOLD_OUT")
                other.connection.execute(
                    "UPDATE platform SET paused_reason=NULL WHERE id='ticketplus'"
                )
                await eventually(lambda: h["source"].calls == 1)
            else:
                with other.transaction() as db:
                    other.enqueue(
                        db, "external", None, "SYSTEM", h["clock"](), {"message": "mock"}, 600, True
                    )
                await eventually(lambda: other.notification_status("external")["status"] == "SENT")
                assert len(h["requests"]) == 1
        finally:
            await cancel(task)
            other.close()

    asyncio.run(scenario())


def test_readonly_store_open_does_not_signal_scheduler_change(harness):
    w = harness["watcher"]
    version = w.store.data_version()
    other = Store(harness["config"].database_path)
    try:
        other.platform()
        assert w.store.data_version() == version
    finally:
        other.close()


@pytest.mark.parametrize("mode", ["retry", "cooldown_expiry", "unconfigured", "inflight"])
def test_delivery_waits_for_retry_lease_or_expiration(harness, monkeypatch, mode):
    h, w = harness, harness["watcher"]
    now, waits = h["clock"](), []
    if mode != "unconfigured":
        enable(h, monkeypatch)
    enqueue(w)
    if mode == "retry":
        h["responses"].append(httpx.Response(500))
    elif mode == "cooldown_expiry":
        w.store.connection.execute(
            "UPDATE platform SET blocked_until=? WHERE id='discord'", (now + 900,)
        )
    elif mode == "inflight":
        assert w.store.claim_notice(now) is not None
    expected = {"retry": 10, "cooldown_expiry": 600, "unconfigured": 600, "inflight": 120}[mode]

    async def scenario():
        stop = asyncio.Event()

        async def wait(changed, deadline):
            waits.append(deadline)
            assert deadline == now + expected
            h["clock"].now = deadline
            stop.set()

        monkeypatch.setattr(w, "_wait_until", wait)
        result = await asyncio.wait_for(w._delivery_loop(asyncio.Event(), stop), 1)
        assert waits == [now + expected]
        status = "SENT" if mode in {"retry", "inflight"} else "EXPIRED"
        assert w.store.notification_status("test-notice")["status"] == status
        assert result["sent"] == (1 if status == "SENT" else 0)

    asyncio.run(scenario())


def test_ready_notification_backlog_does_not_wait_between_batches(harness, monkeypatch):
    h, w = harness, harness["watcher"]
    enable(h, monkeypatch)
    for index in range(25):
        enqueue(w, str(index))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(w._delivery_loop(asyncio.Event(), stop))
        try:
            await eventually(lambda: len(h["requests"]) == 25)
            assert w.store.next_notice_at(h["clock"](), {"default"}) is None
        finally:
            await cancel(task)

    asyncio.run(scenario())
