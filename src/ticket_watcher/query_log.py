"""Compact, private query history: one seven-day file and one previous cycle."""

import json
import logging
import os
from collections import deque
from contextlib import contextmanager

from .models import timestamp

log = logging.getLogger(__name__)
WEEK = 7 * 24 * 60 * 60


class QueryLog:
    def __init__(self, store, database_path, clock):
        self.store, self.clock = store, clock
        self.directory = database_path.with_name(database_path.stem + "-logs")
        self.current = self.directory / "queries.log"
        self.previous = self.directory / "queries.previous.log"
        self.error = None

    @contextmanager
    def _cycle(self):
        """Finish durable rotation before lending the lock to readers/appends."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        while True:
            with self.store.transaction() as db:
                pending = db.execute(
                    "SELECT value FROM runtime WHERE key='query_log_rotation'"
                ).fetchone()
                if pending:
                    plan = json.loads(pending[0])
                    if plan["steps"] == 1 and plan["had_current"]:
                        # If the rename already succeeded before a failure, the
                        # absent current file means previous is the retained week.
                        if self.current.exists():
                            self.current.replace(self.previous)
                    else:
                        self.previous.unlink(missing_ok=True)
                        self.current.unlink(missing_ok=True)
                    db.execute(
                        "INSERT OR REPLACE INTO runtime VALUES('query_log_cycle',?)",
                        (str(plan["start"]),),
                    )
                    db.execute("DELETE FROM runtime WHERE key='query_log_rotation'")
                    continue  # Commit rotation before any fallible read or append.
                now = self.clock()
                row = db.execute("SELECT value FROM runtime WHERE key='query_log_cycle'").fetchone()
                start = float(row[0]) if row else now
                steps = int((now - start) // WEEK)
                if steps > 0:
                    db.execute(
                        "INSERT INTO runtime VALUES('query_log_rotation',?)",
                        (
                            json.dumps(
                                {
                                    "start": start + steps * WEEK,
                                    "steps": steps,
                                    "had_current": self.current.exists(),
                                }
                            ),
                        ),
                    )
                    continue  # Persist intent before changing filesystem state.
                if not row:
                    db.execute("INSERT INTO runtime VALUES('query_log_cycle',?)", (str(start),))
                yield start
                return

    def _failed(self):
        if not self.error:
            log.warning("query_log_io_failed")
        self.error = "無法讀寫查詢紀錄，請檢查資料目錄權限與磁碟空間。"

    def maintain(self):
        try:
            with self._cycle():
                pass
        except OSError:
            self._failed()

    def next_rotation_at(self) -> float:
        rows = dict(
            self.store.connection.execute(
                "SELECT key,value FROM runtime WHERE key IN ('query_log_cycle','query_log_rotation')"
            ).fetchall()
        )
        if "query_log_rotation" in rows or "query_log_cycle" not in rows:
            return self.clock()
        return float(rows["query_log_cycle"]) + WEEK

    def record(self, target_id, mode, result, elapsed):
        # Allowlist only operational fields. Never store URLs, names, messages or credentials.
        data = result.data
        entry = {
            "time": timestamp(self.clock()),
            "target_id": target_id,
            "mode": mode,
            "status": result.execution_status,
            "complete": data.get("complete"),
            "summary": data.get("summary", {}),
            "requests": data.get("request_count", 0),
            "duration_ms": round(elapsed * 1000),
            "reason": data.get("reason") or data.get("error", {}).get("code"),
            "release": bool(data.get("event_id")),
            "hint": bool(data.get("hint_event_id")),
            "next_at": data.get("next_allowed_at"),
        }
        try:
            # Readers/appends share a lock; an I/O failure cannot roll back a
            # rotation whose filesystem changes have already completed.
            with self._cycle():
                with self.current.open("a", encoding="utf-8", newline="\n") as stream:
                    os.chmod(self.current, 0o600)
                    stream.write(
                        json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
            self.error = None
        except OSError:
            self._failed()

    def recent(self, limit=100):
        rows = deque(maxlen=limit)
        try:
            with self._cycle() as start:
                for path in (self.previous, self.current):
                    if not path.exists():
                        continue
                    with path.open("rb") as stream:
                        stream.seek(0, os.SEEK_END)
                        size = stream.tell()
                        stream.seek(max(0, size - 131072))
                        if size > 131072:
                            stream.readline()  # Discard the truncated leading record.
                        for line in stream:
                            try:
                                rows.append(json.loads(line))
                            except (ValueError, UnicodeError):
                                continue  # A crash may leave an incomplete trailing line.
            return {"entries": list(reversed(rows)), "cycle_started_at": timestamp(start)}
        except OSError:
            self._failed()
            return {"entries": [], "cycle_started_at": None}
