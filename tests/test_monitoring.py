import asyncio
import sqlite3
from dataclasses import replace

import pytest
from conftest import URL

from ticket_watcher.models import SourceError
from ticket_watcher.service import Watcher
from ticket_watcher.storage import Store


def check(h, *statuses, **kwargs):
    watcher = h["watcher"]
    state = watcher.store.target("test")
    if state:
        h["clock"].now = max(h["clock"].now, state["next_check"])
    h["source"].push(*statuses, **kwargs)
    return asyncio.run(watcher.check("test"))


def releases(watcher):
    return [e for e in watcher.events(detail=True).data["events"] if e["kind"] == "RELEASE"]


@pytest.mark.parametrize(
    "status",
    ["AVAILABLE", "SOLD_OUT", "TEMPORARILY_UNAVAILABLE", "UPCOMING", "PAUSED", "ENDED", "UNKNOWN"],
)
def test_first_observation_never_reports_release(harness, status):
    result = check(harness, status, complete=status != "UNKNOWN")
    assert result.data["evaluation"]["release_detected"] is False
    assert result.data["evaluation"]["release_hint_detected"] is False
    assert not releases(harness["watcher"])


def test_release_dedup_and_new_release_sequence(harness):
    h = harness
    check(h, "SOLD_OUT")
    result = check(h, "AVAILABLE")
    assert result.data["evaluation"]["release_detected"] is True
    assert result.data["notification"]["status"] == "PENDING"
    assert h["watcher"].store.target("test")["mode"] == "ACTIVE"
    check(h, "AVAILABLE")
    assert len(releases(h["watcher"])) == 1
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    assert len(releases(h["watcher"])) == 2
    assert h["watcher"].store.items("test")[0]["release_sequence"] == 2


@pytest.mark.parametrize("first,expected", [("SOLD_OUT", True), ("AVAILABLE", False)])
def test_unknown_preserves_last_valid_state(harness, first, expected):
    h = harness
    check(h, first)
    old = h["watcher"].store.items("test")[0]
    check(h, "UNKNOWN", complete=False)
    item = h["watcher"].store.items("test")[0]
    assert item["last_valid"] == first and item["valid_at"] == old["valid_at"]
    assert item["observed"] == "UNKNOWN"
    assert check(h, "AVAILABLE").data["evaluation"]["release_detected"] == expected


def test_network_failure_is_unknown_and_restart_preserves_baseline(harness):
    h = harness
    check(h, "SOLD_OUT")
    h["clock"].advance(300)
    h["source"].values.append(SourceError("NETWORK", "timeout"))
    result = asyncio.run(h["watcher"].check("test"))
    assert result.execution_status == "FAILED"
    item = h["watcher"].store.items("test")[0]
    assert item["last_valid"] == "SOLD_OUT" and item["observed"] == "UNKNOWN"
    assert h["watcher"].store.target("test")["next_check"] >= h["clock"]() + 900
    other = Watcher(h["config"], client=h["client"], clock=h["clock"], adapter=h["source"])
    try:
        assert other.store.items("test")[0]["last_valid"] == "SOLD_OUT"
        h["clock"].now = other.store.target("test")["next_check"]
        h["source"].push("AVAILABLE")
        assert asyncio.run(other.check("test")).data["evaluation"]["release_detected"]
    finally:
        other.store.close()


@pytest.mark.parametrize("status", ["AVAILABLE", "TEMPORARILY_UNAVAILABLE"])
def test_query_does_not_change_baseline_or_events(harness, status):
    h = harness
    check(h, "SOLD_OUT")
    before = h["watcher"].store.items("test")
    schedule = h["watcher"].store.target("test")
    h["source"].push(status)
    result = asyncio.run(h["watcher"].query(URL))
    assert result.data["evaluation"] == {"performed": False, "release_detected": None}
    assert result.data["notification"]["status"] == "NOT_APPLICABLE"
    assert before == h["watcher"].store.items("test")
    assert schedule == h["watcher"].store.target("test")
    assert not h["watcher"].events().data["events"]
    assert check(h, "AVAILABLE").data["evaluation"]["release_detected"]


def test_query_blocked_pauses_shared_platform_without_creating_notification(harness):
    h = harness
    h["source"].values.append(SourceError("BLOCKED", "blocked"))
    assert asyncio.run(h["watcher"].query(URL)).execution_status == "FAILED"
    assert h["watcher"].store.platform()["paused_reason"] == "BLOCKED"
    assert not h["watcher"].events().data["events"]
    assert h["watcher"].store.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_cached_status_and_events_never_fetch(harness):
    h = harness
    check(h, "SOLD_OUT")
    h["clock"].advance(600)
    result = h["watcher"].status("test", detail=True)
    assert result.result_source == "CACHE"
    assert result.data["targets"][0]["age_seconds"] == 600
    h["watcher"].events()
    assert h["source"].calls == 1


def test_not_due_tick_never_fetches_again(harness):
    h = harness
    check(h, "SOLD_OUT")
    result = asyncio.run(h["watcher"].tick())
    assert result.data["checks"] == [] and h["source"].calls == 1
    assert h["watcher"].health().data["process_healthy"]
    assert asyncio.run(h["watcher"].check("test")).execution_status == "DEFERRED"


def test_active_does_not_extend_and_exits_after_two_complete_empty_checks(harness):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    expiry = h["watcher"].store.target("test")["active_until"]
    check(h, "AVAILABLE")
    assert h["watcher"].store.target("test")["active_until"] == expiry
    check(h, "SOLD_OUT")
    assert h["watcher"].store.target("test")["no_available"] == 1
    check(h, "UNKNOWN", complete=False)
    assert h["watcher"].store.target("test")["no_available"] == 1
    check(h, "SOLD_OUT")
    assert h["watcher"].store.target("test")["mode"] == "NORMAL"


def test_active_expires_even_when_tickets_stay_available(harness):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    h["clock"].advance(1801)
    check(h, "AVAILABLE")
    assert h["watcher"].store.target("test")["mode"] == "NORMAL"


@pytest.mark.parametrize("baseline", [None, "SOLD_OUT", "AVAILABLE", "UPCOMING", "UNKNOWN"])
def test_unavailable_enters_fast_mode_without_requiring_soldout_baseline(harness, baseline):
    h, w = harness, harness["watcher"]
    if baseline:
        check(h, baseline, complete=baseline != "UNKNOWN")
    result = check(h, "TEMPORARILY_UNAVAILABLE", "SOLD_OUT")
    state = w.store.target("test")
    assert state["mode"] == "ACTIVE" and state["no_available"] == 0
    assert state["active_until"] == h["clock"]() + h["config"].active_window
    assert state["next_check"] == h["clock"]() + 60
    assert not result.data["evaluation"]["release_detected"]
    assert result.data["evaluation"]["release_hint_detected"] is (baseline == "SOLD_OUT")
    if baseline is None:
        assert not w.events().data["events"]


def test_repeated_unavailable_renews_fast_mode_and_real_empty_checks_exit(harness):
    h, w = harness, harness["watcher"]
    check(h, "TEMPORARILY_UNAVAILABLE")
    expiry = w.store.target("test")["active_until"]
    check(h, "SOLD_OUT")
    assert w.store.target("test")["no_available"] == 1
    check(h, "TEMPORARILY_UNAVAILABLE")
    assert w.store.target("test")["no_available"] == 0
    for _ in range(3):
        result = check(h, "TEMPORARILY_UNAVAILABLE")
        assert not result.data["evaluation"]["release_hint_detected"]
        assert w.store.target("test")["no_available"] == 0
    h["clock"].now = expiry + 1
    check(h, "TEMPORARILY_UNAVAILABLE")
    state = w.store.target("test")
    assert state["mode"] == "ACTIVE"
    assert state["active_until"] == h["clock"]() + h["config"].active_window
    assert state["next_check"] == h["clock"]() + 60
    check(h, "SOLD_OUT")
    assert w.store.target("test")["mode"] == "ACTIVE"
    check(h, "SOLD_OUT")
    state = w.store.target("test")
    assert state["mode"] == "NORMAL" and state["active_until"] is None
    assert state["next_check"] == h["clock"]() + 300


def test_unknown_does_not_renew_last_unavailable_signal(harness):
    h, w = harness, harness["watcher"]
    check(h, "TEMPORARILY_UNAVAILABLE")
    expiry = w.store.target("test")["active_until"]
    check(h, "UNKNOWN", complete=False)
    state = w.store.target("test")
    assert state["active_until"] == expiry and state["no_available"] == 0
    assert w.store.items("test")[0]["last_valid"] == "TEMPORARILY_UNAVAILABLE"
    h["clock"].now = expiry + 1
    check(h, "UNKNOWN", complete=False)
    assert w.store.target("test")["mode"] == "NORMAL"


def test_partial_missing_item_cannot_count_as_complete_empty_check(harness):
    h = harness
    check(h, "SOLD_OUT", "SOLD_OUT")
    check(h, "AVAILABLE", "SOLD_OUT")
    result = check(h, "SOLD_OUT")
    assert not result.data["complete"]
    assert h["watcher"].store.target("test")["no_available"] == 0
    assert h["watcher"].store.items("test")[1]["observed"] == "UNKNOWN"


def test_three_parse_failures_pause_and_persist(harness):
    h = harness
    check(h, "SOLD_OUT")
    for _ in range(3):
        h["clock"].now = h["watcher"].store.target("test")["next_check"]
        h["source"].values.append(SourceError("PARSE", "schema changed"))
        asyncio.run(h["watcher"].check("test"))
    assert h["watcher"].store.target("test")["paused_reason"] == "PARSE"
    other = Store(h["config"].database_path)
    try:
        assert other.target("test")["paused_reason"] == "PARSE"
    finally:
        other.close()


def test_429_is_platform_shared_persistent_and_resume_does_not_clear_wait(harness):
    h = harness
    h["source"].values.append(SourceError("RATE_LIMITED", "limited", 7200))
    asyncio.run(h["watcher"].check("test"))
    wait = h["watcher"].store.platform()["blocked_until"]
    assert wait == h["clock"]() + 7200
    h["watcher"].resume(platform=True)
    assert h["watcher"].store.platform()["blocked_until"] == wait
    assert asyncio.run(h["watcher"].query(URL)).execution_status == "DEFERRED"
    other = Store(h["config"].database_path)
    try:
        assert other.platform()["blocked_until"] == wait
    finally:
        other.close()


def test_platform_lease_and_request_gap_are_shared(harness):
    h = harness
    store = h["watcher"].store
    other = Store(h["config"].database_path)
    try:
        assert store.acquire("one", h["clock"](), 180) is None
        assert other.acquire("two", h["clock"](), 180)["lease_owner"] == "one"
        assert store.reserve_request("one", h["clock"](), 5, 180) == 0
        assert store.reserve_request("one", h["clock"](), 5, 180) == 5
        store.release("one")
        assert other.acquire("two", h["clock"](), 180) is None
        assert other.reserve_request("two", h["clock"](), 5, 180) == 5
    finally:
        other.close()


def test_transaction_rolls_back_baseline_if_outbox_insert_fails(harness):
    h = harness
    check(h, "SOLD_OUT")
    h["watcher"].store.connection.execute(
        "CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        check(h, "AVAILABLE")
    assert h["watcher"].store.items("test")[0]["last_valid"] == "SOLD_OUT"
    assert not releases(h["watcher"])
    assert h["watcher"].store.platform()["lease_owner"] is None


def test_source_change_rebaselines_without_release(harness):
    h = harness
    check(h, "SOLD_OUT")
    result = check(h, "AVAILABLE", source="new-source/session")
    assert result.data["evaluation"]["release_detected"] is False


def test_filter_change_resets_baseline_and_cancels_pending(harness):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    target = replace(h["config"].targets[0], session_ids=("s000000001",))
    h["watcher"].config = replace(h["config"], targets=(target,))
    h["source"].push("AVAILABLE")
    result = asyncio.run(h["watcher"].check("test"))
    assert not result.data["evaluation"]["release_detected"]
    assert releases(h["watcher"])[0]["notification_status"] == "CANCELLED"


def test_monitoring_gap_is_recorded(harness):
    h = harness
    check(h, "SOLD_OUT")
    h["clock"].advance(2701)
    assert check(h, "AVAILABLE").data["changes"][0]["monitoring_gap"]


def test_pagination_does_not_reduce_evaluation(harness):
    h = harness
    check(h, *("SOLD_OUT" for _ in range(80)))
    h["clock"].advance(300)
    h["source"].push(*("AVAILABLE" for _ in range(80)))
    result = asyncio.run(h["watcher"].check("test", detail=True, limit=2))
    assert len(result.data["changes"]) == 2 and result.data["changes_total"] == 80
    assert result.data["page"]["total"] == 80
    events = h["watcher"].events(detail=True, limit=100).data["events"]
    assert sum(len(event["payload"]["changes"]) for event in events) == 80
    assert len(result.data["event_ids"]) == 80
    assert len(h["watcher"].store.items("test")) == 80


def test_expired_schedule_runs_once_without_catching_up(harness):
    h = harness
    check(h, "SOLD_OUT")
    h["clock"].advance(86400)
    h["source"].push("SOLD_OUT")
    assert len(asyncio.run(h["watcher"].tick()).data["checks"]) == 1
    assert asyncio.run(h["watcher"].tick()).data["checks"] == []
