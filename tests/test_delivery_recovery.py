import asyncio
import json
from dataclasses import replace

import httpx
from test_monitoring import check, releases
from test_notifications import enable

from ticket_watcher.storage import Store


def test_cancelled_part_does_not_reappear_in_old_event_after_new_release(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT", "SOLD_OUT")
    check(h, "AVAILABLE", "AVAILABLE")
    check(h, "SOLD_OUT", "AVAILABLE")
    check(h, "AVAILABLE", "AVAILABLE")
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    messages = [json.loads(r.content)["content"] for r in h["requests"]]
    assert len(messages) == 2
    assert sum("場次 1" in text for text in messages) == 1
    assert sum("場次 2" in text for text in messages) == 1


def test_notification_retries_are_bounded_at_six_attempts(harness, monkeypatch):
    h = harness
    enable(h, monkeypatch)
    check(h, "SOLD_OUT")
    h["responses"].extend(httpx.Response(500) for _ in range(6))
    check(h, "AVAILABLE")
    for delay in h["config"].retry_delays:
        h["clock"].advance(delay)
        asyncio.run(h["watcher"].notifier.deliver())
    assert len(h["requests"]) == 6
    assert releases(h["watcher"])[0]["notification_status"] == "FAILED"
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(h["requests"]) == 6


def test_crashed_sender_does_not_exceed_attempt_budget(harness):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    event_id = releases(h["watcher"])[0]["id"]
    h["watcher"].store.connection.execute(
        "UPDATE outbox SET attempts=6,status='INFLIGHT',lease_until=? WHERE event_id=?",
        (h["clock"]() - 1, event_id),
    )
    assert h["watcher"].store.claim_notice(h["clock"]()) is None
    assert releases(h["watcher"])[0]["notification_status"] == "FAILED"


def test_sender_claim_is_shared_across_processes(harness):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    other = Store(h["config"].database_path)
    try:
        notice = h["watcher"].store.claim_notice(h["clock"]())
        assert notice is not None
        assert other.claim_notice(h["clock"]()) is None
        h["clock"].advance(121)
        recovered = other.claim_notice(h["clock"]())
        assert recovered["event_id"] == notice["event_id"] and recovered["attempts"] == 2
    finally:
        other.close()


def test_removed_target_cancels_pending_even_without_webhook(harness):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    h["watcher"].config = replace(h["config"], targets=())
    asyncio.run(h["watcher"].tick())
    assert releases(h["watcher"])[0]["notification_status"] == "CANCELLED"


def test_v1_database_migrates_without_losing_baseline_or_pending_work(harness):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    h["watcher"].store.connection.execute("ALTER TABLE outbox DROP COLUMN excluded_items")
    h["watcher"].store.connection.execute("PRAGMA user_version=1")
    other = Store(h["config"].database_path)
    try:
        assert other.items("test")[0]["last_valid"] == "AVAILABLE"
        assert other.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            other.connection.execute(
                "SELECT status,excluded_items FROM outbox WHERE status='PENDING'"
            ).fetchone()[1]
            == "[]"
        )
    finally:
        other.close()


def test_events_default_to_summary_and_full_details_are_explicit(harness):
    h = harness
    check(h, "SOLD_OUT", "SOLD_OUT")
    check(h, "AVAILABLE", "AVAILABLE")
    summary = h["watcher"].events().data["events"][0]["payload"]
    assert summary["changes_total"] == 2 and "changes" not in summary
    assert len(h["watcher"].events(detail=True).data["events"][0]["payload"]["changes"]) == 2
