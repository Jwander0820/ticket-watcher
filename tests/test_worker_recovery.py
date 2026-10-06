import asyncio
import json
import sqlite3

import httpx
import pytest

import ticket_watcher.web as web_module
from ticket_watcher.config import Target
from ticket_watcher.models import Observation, TicketItem, TicketStatus
from ticket_watcher.private_io import atomic_text
from ticket_watcher.service import Watcher
from ticket_watcher.web import Controller

HOOK = "https://discord.com/api/webhooks/123/recovery_test_only"


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


def setup_config(tmp_path, *, enabled=True):
    path = tmp_path / "ui-config.yaml"
    atomic_text(
        path,
        f"""app:
  database_path: watcher.db
notifications:
  webhook_url_env: TICKET_WATCHER_RECOVERY_TEST_UNSET
  worker_alerts_enabled: {str(enabled).lower()}
  worker_alert_channel_id: ops
channels:
  - id: ops
    name: 服務告警
targets: []
""",
    )
    atomic_text(tmp_path / "discord-webhooks.json", json.dumps({"ops": HOOK}))
    return path


@pytest.mark.parametrize("failure", ["poll", "delivery", "return", "cancel"])
def test_worker_restarts_after_failure_without_losing_state_or_spamming(
    tmp_path, monkeypatch, caplog, failure
):
    monkeypatch.setattr(web_module, "WORKER_RETRY_SECONDS", (0.01, 0.02))
    monkeypatch.setattr(web_module, "WORKER_STABLE_SECONDS", 0.04)
    path = setup_config(tmp_path)

    async def scenario():
        instances, requests, stopped = [], [], []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"id": str(len(requests))})

        class FailingWatcher(Watcher):
            async def run(self):
                try:
                    if len(instances) <= 3 and failure == "return":
                        return
                    if len(instances) <= 3 and failure == "cancel":
                        raise asyncio.CancelledError
                    await super().run()
                finally:
                    stopped.append(self)

            async def _poll_loop(self, wakeup):
                if len(instances) <= 3 and failure == "poll":
                    raise RuntimeError("must_not_leak_private_exception")
                await super()._poll_loop(wakeup)

            async def _delivery_loop(self, wakeup, stop):
                if len(instances) <= 3 and failure == "delivery":
                    raise RuntimeError("must_not_leak_private_exception")
                await super()._delivery_loop(wakeup, stop)

        def factory(config):
            # Each restart opens a fresh connection/client, including the HTTP pool.
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            watcher = FailingWatcher(config, client=client)
            watcher._owns_client = True
            instances.append(watcher)
            return watcher

        c = Controller(path, watcher_factory=factory)
        await c.start()
        target = Target("preserved", "保留基準", "https://ticketplus.com.tw/activity/e000000001")
        c.watcher.store.sync_target(target)
        c.watcher._apply(
            target,
            Observation(
                "保留基準",
                target.url,
                c.watcher.clock(),
                (
                    TicketItem(
                        "s000000001", "s000000001", "場次", TicketStatus.SOLD_OUT, target.url
                    ),
                ),
            ),
        )
        baseline = c.watcher.store.items(target.id)
        due = c.watcher.store.target(target.id)["next_check"]
        c.watcher.store.connection.execute(
            "UPDATE platform SET paused_reason='BLOCKED',blocked_until=9999999999 WHERE id='ticketplus'"
        )
        c.watcher.store.connection.execute(
            "INSERT INTO runtime VALUES('preserved_test_state','baseline')"
        )
        try:
            await eventually(lambda: c.restart_count == 3 and c.runner_error is None)
            await c.watcher.notifier.deliver()  # Flush recovery event without waiting 5 seconds.
            assert c.task and not c.task.done()
            assert len(instances) == 4 and stopped == instances[:3]
            assert all(w.client.is_closed for w in instances[:3])
            for watcher in instances[:3]:
                with pytest.raises(sqlite3.ProgrammingError):
                    watcher.store.connection.execute("SELECT 1")
            platform = c.watcher.store.platform()
            assert platform["paused_reason"] == "BLOCKED"
            assert platform["blocked_until"] == 9999999999
            assert c.watcher.store.items(target.id) == baseline
            assert c.watcher.store.target(target.id)["next_check"] == due
            assert (
                c.watcher.store.connection.execute(
                    "SELECT value FROM runtime WHERE key='preserved_test_state'"
                ).fetchone()[0]
                == "baseline"
            )
            events = c.watcher.events(detail=True).data["events"]
            assert len(events) == 2
            assert {e["notification_status"] for e in events} == {"SENT"}
            assert len(requests) == 2
            assert all(str(r.url).split("?")[0] == HOOK for r in requests)
            assert "must_not_leak_private_exception" not in caplog.text
            assert HOOK not in json.dumps(c.state())
        finally:
            await c.close()
        assert len(stopped) == 4 and all(w.client.is_closed for w in instances)
        assert len(requests) == 2  # Intentional shutdown never creates an incident.

    asyncio.run(scenario())


@pytest.mark.parametrize("discord_behavior", ["offline", "hang", "notifier_bug"])
def test_discord_failure_does_not_block_recovery(tmp_path, monkeypatch, discord_behavior):
    monkeypatch.setattr(web_module, "WORKER_RETRY_SECONDS", (0.02,))
    monkeypatch.setattr(web_module, "WORKER_STABLE_SECONDS", 0.02)
    path = setup_config(tmp_path)

    async def scenario():
        instances = []

        async def respond(request):
            if discord_behavior == "offline":
                raise httpx.ConnectError("private-url", request=request)
            if discord_behavior == "hang":
                await asyncio.Event().wait()
            raise RuntimeError("private-notifier-error")

        class FailingWatcher(Watcher):
            async def _poll_loop(self, wakeup):
                if len(instances) == 1:
                    raise RuntimeError
                await super()._poll_loop(wakeup)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

            def factory(config):
                w = FailingWatcher(config, client=client)
                instances.append(w)
                return w

            c = Controller(path, watcher_factory=factory)
            await c.start()
            try:
                await eventually(lambda: c.restart_count >= 1)
                assert c.task and not c.task.done()
                assert len(c.watcher.events().data["events"]) >= 1
            finally:
                await c.close()

    asyncio.run(scenario())


def test_constructor_failure_retries_and_cancel_during_backoff_stops_all_work(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(web_module, "WORKER_RETRY_SECONDS", (0.01, 0.02, 30))
    path = setup_config(tmp_path, enabled=False)

    async def scenario():
        calls = 0

        class FailingWatcher(Watcher):
            async def run(self):
                raise RuntimeError

        def factory(config):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("constructor-private-error")
            return FailingWatcher(config)

        c = Controller(path, watcher_factory=factory)
        await c.start()
        try:
            await eventually(lambda: calls == 3)
            state = c.state()  # The failed replacement never leaves a closed DB behind.
            assert state["runner_error"] and not state["health"]["process_healthy"]
            events = c.watcher.events().data["events"]
            assert len(events) == 1 and events[0]["notification_status"] == "DISABLED"
            client = c.watcher.client
        finally:
            await c.close()
        await asyncio.sleep(0.04)
        assert calls == 3 and client.is_closed

    asyncio.run(scenario())
