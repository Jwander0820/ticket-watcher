import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from conftest import URL, Clock, Source
from test_ui_pause import control
from test_web import snapshot

from ticket_watcher.config import load_config
from ticket_watcher.service import Watcher
from ticket_watcher.storage import Store
from ticket_watcher.web import CONTROLLER, create_app


def overdue_targets(h, count=4):
    w = h["watcher"]
    targets = tuple(replace(w.config.targets[0], id=f"target-{i}") for i in range(count))
    w.config = replace(w.config, targets=targets)
    for target in targets:
        w.store.sync_target(target)
    w.store.connection.execute("UPDATE targets SET next_check=?", (h["clock"]() - 86400,))
    return targets


def deadlines(w):
    return {t.id: w.store.target(t.id)["next_check"] for t in w.config.targets}


def assert_spread(values, start):
    previous = start
    for due in sorted(values):
        assert 60 <= due - previous <= 90
        previous = due


@asynccontextmanager
async def running_until_idle(w, monkeypatch):
    idle = asyncio.Event()

    async def wait(changed, deadline):
        if changed in w._poll_waiters:
            idle.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(w, "_wait_until", wait)
    task = asyncio.create_task(w.run())
    try:
        await asyncio.wait_for(idle.wait(), 1)
        yield
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_restart_spreads_overdue_queries_and_persists_deadlines(harness, monkeypatch):
    h, w = harness, harness["watcher"]
    overdue_targets(h)
    for _ in range(4):
        h["source"].push("SOLD_OUT")

    async def scenario():
        async with running_until_idle(w, monkeypatch):
            assert h["source"].calls == 0
            scheduled = deadlines(w)
            assert_spread(scheduled.values(), h["clock"]())

        async with Watcher(
            w.config,
            client=h["client"],
            clock=h["clock"],
            rng=random.Random(99),
            adapter=h["source"],
        ) as restarted:
            async with running_until_idle(restarted, monkeypatch):
                assert deadlines(restarted) == scheduled
                assert h["source"].calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("gate", ["blocked_until", "lease_until", "next_request"])
def test_platform_resume_spreads_after_existing_gate_without_clearing_it(harness, gate):
    h, w = harness, harness["watcher"]
    overdue_targets(h)
    gate_until = h["clock"]() + 900
    w.store.connection.execute(
        f"UPDATE platform SET paused_reason='BLOCKED', {gate}=? WHERE id='ticketplus'",
        (gate_until,),
    )
    w.resume(platform=True)
    scheduled = deadlines(w)
    assert_spread(scheduled.values(), gate_until)
    assert w.store.platform()[gate] == gate_until
    assert w.store.platform()["paused_reason"] is None
    w.resume(platform=True)
    assert deadlines(w) == scheduled  # Repeated resume must not postpone work again.
    assert h["source"].calls == 0


def test_only_first_recovery_deadline_is_queried_then_regular_polling_resumes(harness):
    h, w = harness, harness["watcher"]
    overdue_targets(h)
    w.store.connection.execute("UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'")
    w.resume(platform=True)
    scheduled = deadlines(w)
    first = min(scheduled, key=scheduled.get)
    h["source"].push("SOLD_OUT")

    async def scenario():
        assert await w._check_due(asyncio.Event()) == []
        h["clock"].now = scheduled[first]
        results = await w._check_due(asyncio.Event())
        assert len(results) == 1 and results[0].execution_status == "COMPLETED"
        assert h["source"].calls == 1
        assert w.store.target(first)["next_check"] == h["clock"]() + 300
        assert all(deadlines(w)[key] == due for key, due in scheduled.items() if key != first)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "excluded", ["future", "disabled", "paused", "stopped", "show_started", "changed"]
)
def test_recovery_keeps_ineligible_targets_and_all_other_state(harness, excluded):
    h, w = harness, harness["watcher"]
    first, other = overdue_targets(h, 2)
    if excluded == "future":
        w.store.connection.execute(
            "UPDATE targets SET next_check=?, last_error='NETWORK', failures=2 WHERE id=?",
            (h["clock"]() + 1200, other.id),
        )
    elif excluded == "disabled":
        w.config = replace(w.config, targets=(first, replace(other, enabled=False)))
    elif excluded == "paused":
        w.store.connection.execute(
            "UPDATE targets SET paused_reason='PARSE' WHERE id=?", (other.id,)
        )
    elif excluded == "stopped":
        w.config = replace(w.config, targets=(first, replace(other, stop_at=h["clock"]() - 1)))
    elif excluded == "show_started":
        with w.store.transaction() as db:
            w.store.save_schedule(db, other.id, {"session": h["clock"]() - 1})
    else:
        w.store.connection.execute(
            "UPDATE targets SET signature='old-selection' WHERE id=?", (other.id,)
        )
    before = {t.id: w.store.target(t.id) for t in w.config.targets}
    w.prepare_monitoring()
    assert w.store.target(other.id) == before[other.id]
    updated = w.store.target(first.id)
    assert updated.pop("next_check") > h["clock"]()
    before[first.id].pop("next_check")
    assert updated == before[first.id]


def test_paused_platform_does_not_reschedule_until_explicit_resume(harness, monkeypatch):
    h, w = harness, harness["watcher"]
    overdue_targets(h)
    w.store.connection.execute("UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'")
    original = deadlines(w)

    async def scenario():
        async with running_until_idle(w, monkeypatch):
            assert deadlines(w) == original
            assert h["source"].calls == 0
            w.resume(platform=True)
            assert_spread(deadlines(w).values(), h["clock"]())

    asyncio.run(scenario())


def test_target_resume_only_spreads_the_target_that_was_paused(harness):
    h, w = harness, harness["watcher"]
    first, other = overdue_targets(h, 2)
    w.store.connection.execute("UPDATE targets SET paused_reason='PARSE' WHERE id=?", (first.id,))
    old_other = w.store.target(other.id)
    w.resume(first.id)
    assert_spread([w.store.target(first.id)["next_check"]], h["clock"]())
    assert w.store.target(other.id) == old_other


def test_recovery_preserves_baseline_and_explicit_manual_query(harness):
    h, w = harness, harness["watcher"]

    async def scenario():
        h["source"].push("SOLD_OUT")
        await w._check("test", immediate=True)
        items = w.store.items("test")
        h["clock"].advance(86400)
        w.prepare_monitoring()
        assert w.store.items("test") == items
        h["source"].push("AVAILABLE")
        result = await w._check("test", immediate=True, mode="manual")
        assert result.data["evaluation"]["release_detected"]

    asyncio.run(scenario())


@pytest.mark.parametrize("platform_paused", [False, True])
def test_ui_stays_paused_across_restart_then_spreads_on_resume(tmp_path, platform_paused):
    clock = Clock()
    source = Source(clock)
    path = tmp_path / "ui-config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "app": {"ui_paused": True, "database_path": "watcher.db"},
                "notifications": {"system_alerts_enabled": False},
                "targets": [{"id": f"target-{i}", "name": str(i), "url": URL} for i in range(4)],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    store = Store(config.database_path)
    for target in config.targets:
        store.sync_target(target)
    store.connection.execute("UPDATE targets SET next_check=?", (clock() - 86400,))
    if platform_paused:
        store.connection.execute(
            "UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'"
        )
    store.close()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:

            def app():
                return create_app(
                    path,
                    watcher_factory=lambda cfg: Watcher(
                        cfg,
                        client=client,
                        clock=clock,
                        rng=random.Random(7),
                        adapter=source,
                    ),
                )

            for _ in range(2):
                application = app()
                async with TestClient(TestServer(application)) as panel:
                    c = application[CONTROLLER]
                    state = await snapshot(panel)
                    assert state["ui_paused"] and not state["runner_active"]
                    assert all(due == clock() - 86400 for due in deadlines(c.watcher).values())
                    assert source.calls == 0

            application = app()
            async with TestClient(TestServer(application)) as panel:
                c = application[CONTROLLER]
                response = await control(panel, await snapshot(panel), "resume-service")
                assert response.status == 200
                state = await response.json()
                if platform_paused:
                    assert all(due < clock() for due in deadlines(c.watcher).values())
                    response = await control(panel, state, "resume", platform=True)
                    assert response.status == 200
                scheduled = deadlines(c.watcher)
                assert_spread(scheduled.values(), clock())
                assert source.calls == 0

            application = app()
            async with TestClient(TestServer(application)):
                assert deadlines(application[CONTROLLER].watcher) == scheduled
                assert source.calls == 0

    asyncio.run(scenario())
