import asyncio
from dataclasses import replace
from datetime import datetime

import pytest
from test_notifications import enable
from test_web import mutate, panel, snapshot

from ticket_watcher.config import parse_config
from ticket_watcher.schedule import session_start
from ticket_watcher.service import Watcher


@pytest.mark.parametrize("missing", [False, True])
def test_started_session_unknown_does_not_pause_remaining_sessions(harness, missing):
    from test_ticketplus import SampleTransport

    from ticket_watcher.platforms.ticketplus import TicketPlusAdapter

    h, w = harness, harness["watcher"]

    class TwoSessions(SampleTransport):
        async def get_json(self, url, params):
            if params.get("path", "").endswith("sessions.json"):
                return {
                    "sessions": [
                        {"sessionId": "s000001778", "date": "2020-01-01", "time": "19:00"},
                        {"sessionId": "s000001779", "date": "2099-01-01", "time": "19:00"},
                    ]
                }
            return await super().get_json(url, params)

    transport = TwoSessions(
        h,
        [
            {"id": "s000001778", "status": "soldout"},
            {"id": "s000001779", "status": "soldout"},
        ],
    )
    w.adapter = TicketPlusAdapter(transport)

    async def scenario():
        await w._check("test", immediate=True)
        transport.statuses = [{"id": "s000001779", "status": "soldout"}]
        if not missing:
            transport.statuses.append({"id": "s000001778", "status": "unknown_status"})
        # Standalone queries still report the complete source, including past shows.
        assert not (await w.query(w.config.targets[0].url)).data["complete"]
        for _ in range(3):
            h["clock"].now = w.store.target("test")["next_check"]
            result = await w._check("test", immediate=True)
            assert result.data["complete"]
        state = w.store.target("test")
        assert state["paused_reason"] is None and state["parse_failures"] == 0
        transport.statuses[0]["status"] = "onsale"
        result = await w._check("test", immediate=True)
        assert result.data["evaluation"]["release_detected"]
        assert [change["item_key"] for change in result.data["changes"]] == ["s000001779"]

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["future_unknown", "auto_stop_disabled", "unspecified_partial"])
def test_auto_stop_preserves_other_incomplete_data_protection(harness, case):
    h, w = harness, harness["watcher"]
    times = {"s000000001": h["clock"]() - 10, "s000000002": h["clock"]() + 86400}

    class PartialSource:
        async def fetch(self, target):
            observation = await h["source"].fetch(target)
            return replace(
                observation,
                session_starts=times,
                incomplete_session_ids=None
                if case == "unspecified_partial"
                else frozenset(
                    item.session_id for item in observation.items if item.status == "UNKNOWN"
                ),
            )

    w.adapter = PartialSource()
    if case == "auto_stop_disabled":
        w.config = replace(w.config, targets=(replace(w.config.targets[0], auto_stop=False),))
    statuses = ("SOLD_OUT", "UNKNOWN") if case == "future_unknown" else ("UNKNOWN", "SOLD_OUT")
    if case == "unspecified_partial":
        statuses = ("SOLD_OUT", "SOLD_OUT")
    for _ in range(3):
        state = w.store.target("test")
        if state:
            h["clock"].now = state["next_check"]
        h["source"].push(*statuses, complete=False)
        result = asyncio.run(w._check("test", immediate=True))
        assert not result.data["complete"]
    assert w.store.target("test")["paused_reason"] == "PARSE"


@pytest.mark.parametrize(
    "date,time,expected",
    [
        ("2026-10-09", "17:00", "2026-10-09T17:00:00+08:00"),
        ("2026-10-09 ~ 2026-10-09", "19:30 ~ 21:00", "2026-10-09T19:30:00+08:00"),
        ("2026-10-09 ~ 2026-10-10", "23:30 ~ 01:00", "2026-10-09T23:30:00+08:00"),
        ("2026-10-09", "17:00:30", "2026-10-09T17:00:30+08:00"),
    ],
)
def test_provider_start_time_is_parsed_in_taipei(date, time, expected):
    assert session_start(date, time) == datetime.fromisoformat(expected).timestamp()


@pytest.mark.parametrize(
    "date,time",
    [
        (None, "17:00"),
        ("2026-10-09", None),
        ("2026-10-09", "待公布"),
        ("2026-02-30", "17:00"),
        ("2026-10-09", "25:00"),
        ("2026-10-09 ~ unknown", "17:00"),
        ("2026-10-09", "17:00 ~ invalid"),
        ("2026-10-09", "19:00 ~ 17:00"),
        ("10/09", "17:00"),
    ],
)
def test_uncertain_source_time_does_not_create_a_deadline(date, time):
    assert session_start(date, time) is None


def scheduled_source(harness, times):
    h = harness

    class ScheduledSource:
        async def fetch(self, target):
            return replace(await h["source"].fetch(target), session_starts=dict(times))

    h["watcher"].adapter = ScheduledSource()


def observe(h, *statuses):
    h["source"].push(*statuses)
    return asyncio.run(h["watcher"]._check("test", immediate=True))


def test_last_selected_show_stops_checks_and_survives_restart(harness):
    h, w = harness, harness["watcher"]
    now = h["clock"]()
    scheduled_source(h, {"s000000001": now + 60, "s000000002": now + 120})
    observe(h, "SOLD_OUT", "SOLD_OUT")
    h["clock"].advance(60)
    result = observe(h, "AVAILABLE", "AVAILABLE")
    assert result.data["evaluation"]["release_detected"]
    assert [x["item_key"] for x in result.data["changes"]] == ["s000000002"]
    state = w.status("test").data["targets"][0]
    assert state["stopped_session_count"] == 1 and state["stop_reason"] is None
    h["clock"].advance(60)
    assert asyncio.run(w.check("test", immediate=True)).data["reason"] == "SHOW_STARTED"
    assert asyncio.run(w.tick()).data["checks"] == []
    assert w.events().data["events"][0]["notification_status"] == "CANCELLED"
    assert h["source"].calls == 2
    other = Watcher(h["config"], client=h["client"], clock=h["clock"], adapter=h["source"])
    try:
        assert asyncio.run(other.check("test", immediate=True)).data["reason"] == "SHOW_STARTED"
        assert other.status("test").data["targets"][0]["stop_reason"] == "SHOW_STARTED"
        assert h["source"].calls == 2
    finally:
        other.store.close()


def test_unknown_session_time_keeps_remaining_target_running(harness):
    h, w = harness, harness["watcher"]
    scheduled_source(h, {"s000000001": h["clock"]() - 1, "s000000002": None})
    observe(h, "SOLD_OUT", "SOLD_OUT")
    result = observe(h, "AVAILABLE", "AVAILABLE")
    assert [x["item_key"] for x in result.data["changes"]] == ["s000000002"]
    assert w.status("test").data["targets"][0]["effective_stop_at"] is None


def test_started_session_unavailable_does_not_enable_or_renew_fast_mode(harness):
    h, w = harness, harness["watcher"]
    now = h["clock"]()
    scheduled_source(h, {"s000000001": now - 1, "s000000002": now + 86400})
    observe(h, "TEMPORARILY_UNAVAILABLE", "SOLD_OUT")
    assert w.store.target("test")["mode"] == "NORMAL"
    observe(h, "TEMPORARILY_UNAVAILABLE", "AVAILABLE")
    expiry = w.store.target("test")["active_until"]
    h["clock"].advance(60)
    observe(h, "TEMPORARILY_UNAVAILABLE", "SOLD_OUT")
    state = w.store.target("test")
    assert state["active_until"] == expiry and state["no_available"] == 1
    h["clock"].advance(60)
    observe(h, "TEMPORARILY_UNAVAILABLE", "SOLD_OUT")
    assert w.store.target("test")["mode"] == "NORMAL"


def test_start_time_crossed_during_request_does_not_enqueue_release(harness):
    h, w = harness, harness["watcher"]
    deadline = h["clock"]() + 10
    scheduled_source(h, {"s000000001": deadline})
    observe(h, "SOLD_OUT")

    class CrossingSource:
        async def fetch(self, target):
            h["clock"].advance(11)
            return replace(await h["source"].fetch(target), session_starts={"s000000001": deadline})

    w.adapter = CrossingSource()
    result = observe(h, "AVAILABLE")
    assert result.data["stop_reason"] == "SHOW_STARTED"
    assert not result.data["evaluation"]["release_detected"]
    assert not w.events().data["events"]


def test_queued_notification_omits_started_session(harness, monkeypatch):
    h, w = harness, harness["watcher"]
    now = h["clock"]()
    scheduled_source(h, {"s000000001": now + 10, "s000000002": now + 100})
    observe(h, "SOLD_OUT", "SOLD_OUT")
    observe(h, "AVAILABLE", "AVAILABLE")
    h["clock"].advance(10)
    enable(h, monkeypatch)
    asyncio.run(w.notifier.deliver())
    content = h["requests"][0].read().decode()
    assert "s000000001" not in content and "s000000002" in content


def test_auto_stop_can_be_disabled_without_resetting_baseline(harness):
    h, w = harness, harness["watcher"]
    scheduled_source(h, {"s000000001": h["clock"]() + 10})
    observe(h, "SOLD_OUT")
    h["clock"].advance(10)
    target = replace(h["config"].targets[0], auto_stop=False)
    w.config = replace(h["config"], targets=(target,))
    result = observe(h, "AVAILABLE")
    assert result.data["evaluation"]["release_detected"]
    assert w.status("test").data["targets"][0]["auto_stop_at"] is None


def test_manual_stop_uses_earlier_deadline(harness):
    h, w = harness, harness["watcher"]
    now = h["clock"]()
    scheduled_source(h, {"s000000001": now + 100})
    target = replace(h["config"].targets[0], stop_at=now + 10)
    w.config = replace(h["config"], targets=(target,))
    observe(h, "SOLD_OUT")
    h["clock"].advance(10)
    assert w.status("test").data["targets"][0]["stop_reason"] == "STOP_AT"
    assert asyncio.run(w.check("test", immediate=True)).execution_status == "DEFERRED"
    assert h["source"].calls == 1


def test_changed_filters_do_not_inherit_old_stop_time(harness):
    h, w = harness, harness["watcher"]
    times = {"s000000001": h["clock"]() - 10}
    scheduled_source(h, times)
    observe(h, "SOLD_OUT")
    target = replace(h["config"].targets[0], session_ids=("s000000002",))
    w.config = replace(h["config"], targets=(target,))
    assert w.status("test").data["targets"][0]["stop_reason"] is None
    times.clear()
    times["s000000002"] = h["clock"]() + 500
    assert observe(h, "SOLD_OUT").execution_status == "COMPLETED"
    assert w.store.target_schedule(target)["sessions"] == times


def test_updated_session_time_replaces_persisted_deadline(harness):
    h, w = harness, harness["watcher"]
    now = h["clock"]()
    times = {"s000000001": now + 60}
    scheduled_source(h, times)
    observe(h, "SOLD_OUT")
    times["s000000001"] = now + 600
    observe(h, "SOLD_OUT")
    h["clock"].advance(60)
    assert observe(h, "SOLD_OUT").execution_status == "COMPLETED"
    assert w.store.target_schedule(w.config.targets[0])["stop_at"] == now + 600


def test_single_query_does_not_save_monitoring_deadline(harness):
    h, w = harness, harness["watcher"]
    scheduled_source(h, {"s000000001": h["clock"]() - 10})
    h["source"].push("SOLD_OUT")
    assert asyncio.run(w.query(h["config"].targets[0].url)).execution_status == "COMPLETED"
    assert w.store.connection.execute("SELECT count(*) FROM target_schedules").fetchone()[0] == 0


def test_ui_preserves_auto_stop_setting_and_shows_deadline(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            response = await mutate(
                client,
                await snapshot(client),
                "/api/targets",
                {
                    "name": "test",
                    "url": "https://ticketplus.com.tw/activity/e000000001",
                },
            )
            state = await response.json()
            assert state["targets"][0]["auto_stop"] is True
            target = c.config.targets[0]
            c.watcher.store.sync_target(target)
            with c.watcher.store.transaction() as db:
                c.watcher.store.save_schedule(db, target.id, {"s000000001": c.watcher.clock() - 10})
            assert (await snapshot(client))["targets"][0]["state"]["stop_reason"] == "SHOW_STARTED"
            response = await mutate(
                client,
                state,
                f"/api/targets/{target.id}",
                {
                    "name": target.name,
                    "auto_stop": False,
                },
                method="PUT",
            )
            updated = await response.json()
            assert response.status == 200 and updated["targets"][0]["auto_stop"] is False
            assert updated["targets"][0]["state"]["stop_reason"] is None
            assert not requests

    asyncio.run(scenario())


def test_auto_stop_config_requires_boolean(tmp_path):
    with pytest.raises(ValueError):
        parse_config(
            {
                "targets": [
                    {
                        "id": "test",
                        "url": "https://ticketplus.com.tw/activity/e000000001",
                        "auto_stop": "false",
                    }
                ]
            },
            tmp_path,
        )
