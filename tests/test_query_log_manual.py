import asyncio
import json

import pytest

from ticket_watcher.models import Result, SourceError
from ticket_watcher.query_log import WEEK, QueryLog


def test_log_two_cycles_survive_restart_and_expire_without_new_queries(harness):
    h = harness
    journal = h["watcher"].query_log
    result = Result(data={"complete": True, "summary": {"SOLD_OUT": 1}, "request_count": 1})
    journal.record("test", "scheduled", result, 0.125)
    first_cycle = journal.recent()["cycle_started_at"]
    h["clock"].advance(WEEK - 1)
    restarted = QueryLog(h["watcher"].store, h["config"].database_path, h["clock"])
    restarted.record("test", "scheduled", result, 0.25)
    assert restarted.recent()["cycle_started_at"] == first_cycle
    assert len(restarted.recent()["entries"]) == 2
    h["clock"].advance(1)
    restarted.record("test", "manual", result, 0.5)
    assert len(restarted.previous.read_text().splitlines()) == 2
    assert len(restarted.current.read_text().splitlines()) == 1
    assert len(restarted.recent()["entries"]) == 3
    h["clock"].advance(WEEK)
    restarted.record("test", "manual", result, 1)
    assert len(restarted.recent()["entries"]) == 2
    assert len(list(restarted.directory.iterdir())) == 2
    h["clock"].advance(2 * WEEK)
    restarted.maintain()
    assert restarted.recent()["entries"] == []
    assert not list(restarted.directory.iterdir())


def test_log_allowlist_and_tail_read(harness):
    journal = harness["watcher"].query_log
    result = Result(
        "FAILED",
        data={
            "error": {"code": "NETWORK", "message": "private-message"},
            "public_url": "private-url",
            "event_name": "private-name",
            "webhook_url": "private-hook",
            "request_count": 1,
        },
    )
    for _ in range(110):
        journal.record("test", "manual", result, 0.1)
    assert "private-" not in journal.current.read_text()
    assert len(journal.recent()["entries"]) == 100
    with journal.current.open("a") as stream:
        stream.write('{"incomplete":')
    assert len(journal.recent()["entries"]) == 100


def test_log_write_failure_does_not_stop_monitoring(harness):
    h = harness
    journal = h["watcher"].query_log
    journal.directory.write_text("not a directory")
    h["source"].push("SOLD_OUT")
    result = asyncio.run(h["watcher"].check("test", immediate=True))
    assert result.execution_status == "COMPLETED"
    assert journal.error


def test_successful_read_does_not_hide_a_log_write_failure(harness, monkeypatch):
    journal = harness["watcher"].query_log
    result = Result(data={"complete": True})
    journal.record("test", "manual", result, 0.1)
    path_type = type(journal.current)
    original = path_type.open

    def open_file(path, mode="r", *args, **kwargs):
        if path == journal.current and mode == "a":
            raise PermissionError
        return original(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(path_type, "open", open_file)
        journal.record("test", "manual", result, 0.1)
        journal.maintain()
        assert len(journal.recent()["entries"]) == 1 and journal.error
    journal.record("test", "manual", result, 0.1)
    assert journal.error is None


def test_immediate_check_bypasses_schedule_preserves_baseline_and_records_result(harness):
    h = harness
    w = h["watcher"]
    h["source"].push("SOLD_OUT", "SOLD_OUT")
    asyncio.run(w.check("test"))
    before = w.store.items("test")
    assert w.store.target("test")["next_check"] > h["clock"]()
    assert asyncio.run(w.check("test")).execution_status == "DEFERRED"
    h["source"].push("SOLD_OUT", "AVAILABLE")
    result = asyncio.run(w.check("test", immediate=True))
    assert result.execution_status == "COMPLETED"
    assert result.data["evaluation"]["release_detected"]
    assert h["source"].calls == 2
    assert w.store.items("test")[0] == before[0]
    entries = w.query_log.recent()["entries"]
    assert entries[0]["mode"] == "manual" and entries[0]["release"]
    assert entries[0]["summary"] == {"SOLD_OUT": 1, "AVAILABLE": 1}
    assert entries[1]["status"] == "DEFERRED"
    count = len(entries)
    asyncio.run(w._check("test"))
    assert len(w.query_log.recent()["entries"]) == count


@pytest.mark.parametrize("block", ["cooldown", "platform_pause", "target_pause", "busy", "failure"])
def test_immediate_check_preserves_all_safety_waits(harness, block):
    h = harness
    w = h["watcher"]
    h["source"].push("SOLD_OUT")
    asyncio.run(w.check("test"))
    db = w.store.connection
    now = h["clock"]()
    if block == "cooldown":
        db.execute("UPDATE platform SET blocked_until=? WHERE id='ticketplus'", (now + 120,))
    elif block == "platform_pause":
        db.execute("UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'")
    elif block == "target_pause":
        db.execute("UPDATE targets SET paused_reason='PARSE' WHERE id='test'")
    elif block == "busy":
        w.store.acquire("other", now, 180)
    else:
        w._error(SourceError("NETWORK", "test failure"), w.config.targets[0])
    result = asyncio.run(w.check("test", immediate=True))
    assert result.execution_status == "DEFERRED"
    assert h["source"].calls == 1
    assert (
        result.data["reason"]
        == {
            "failure": "TARGET_BACKOFF",
            "cooldown": "PLATFORM_BUSY_OR_BACKOFF",
            "busy": "PLATFORM_BUSY_OR_BACKOFF",
            "platform_pause": "PLATFORM_PAUSED",
            "target_pause": "TARGET_PAUSED",
        }[block]
    )
    assert w.query_log.recent()["entries"][0]["status"] == "DEFERRED"


def test_failed_and_partial_queries_are_logged(harness):
    h = harness
    h["source"].push("UNKNOWN", complete=False)
    asyncio.run(h["watcher"].check("test", immediate=True))
    entries = h["watcher"].query_log.recent()["entries"]
    assert not entries[0]["complete"] and entries[0]["summary"] == {"UNKNOWN": 1}
    h["clock"].advance(3600)
    h["source"].values.append(SourceError("NETWORK", "safe error"))
    asyncio.run(h["watcher"].check("test", immediate=True))
    entry = h["watcher"].query_log.recent()["entries"][0]
    assert entry["status"] == "FAILED" and entry["reason"] == "NETWORK"
    assert "safe error" not in json.dumps(entry)
