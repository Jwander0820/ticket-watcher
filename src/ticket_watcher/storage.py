import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import Target

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
 id TEXT PRIMARY KEY, signature TEXT NOT NULL, name TEXT NOT NULL,
 mode TEXT NOT NULL DEFAULT 'NORMAL', next_check REAL NOT NULL DEFAULT 0,
 active_until REAL, no_available INTEGER NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, parse_failures INTEGER NOT NULL DEFAULT 0,
 paused_reason TEXT, last_checked REAL, last_success REAL, last_error TEXT,
 source TEXT, event_name TEXT, public_url TEXT, request_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
 target_id TEXT NOT NULL REFERENCES targets(id), item_key TEXT NOT NULL,
 last_valid TEXT, valid_at REAL, observed TEXT NOT NULL, observed_at REAL NOT NULL,
 release_sequence INTEGER NOT NULL DEFAULT 0, details TEXT NOT NULL,
 PRIMARY KEY(target_id, item_key)
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, target_id TEXT, kind TEXT NOT NULL,
 created_at REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
 event_id TEXT PRIMARY KEY REFERENCES events(id), status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, expires_at REAL NOT NULL,
 lease_until REAL, message_id TEXT, last_error TEXT,
 excluded_items TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS platform (
 id TEXT PRIMARY KEY, next_request REAL NOT NULL DEFAULT 0,
 blocked_until REAL NOT NULL DEFAULT 0, paused_reason TEXT,
 lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
 request_count INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO platform(id) VALUES ('ticketplus');
INSERT OR IGNORE INTO platform(id) VALUES ('discord');
CREATE TABLE IF NOT EXISTS metadata (
 key TEXT PRIMARY KEY, expires_at REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_attempt);
CREATE INDEX IF NOT EXISTS events_created ON events(created_at);
PRAGMA user_version=2;
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.connection.close()
            raise ValueError("資料庫版本不支援")
        if version == 1:
            with self.transaction() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
                if "excluded_items" not in columns:
                    db.execute(
                        "ALTER TABLE outbox ADD COLUMN excluded_items TEXT NOT NULL DEFAULT '[]'"
                    )
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def target(self, ident: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM targets WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else None

    def sync_target(self, target: Target):
        with self.transaction() as db:
            old = db.execute("SELECT * FROM targets WHERE id=?", (target.id,)).fetchone()
            if old and old["signature"] != target.signature:
                db.execute("DELETE FROM items WHERE target_id=?", (target.id,))
                db.execute(
                    """UPDATE outbox SET status='CANCELLED' WHERE event_id IN
                 (SELECT id FROM events WHERE target_id=?) AND status IN ('PENDING','INFLIGHT')""",
                    (target.id,),
                )
                db.execute("DELETE FROM targets WHERE id=?", (target.id,))
            db.execute(
                """INSERT INTO targets(id,signature,name,public_url) VALUES(?,?,?,?)
             ON CONFLICT(id) DO UPDATE SET name=excluded.name, public_url=excluded.public_url""",
                (target.id, target.signature, target.name, target.url),
            )

    def items(self, ident: str) -> list[dict]:
        return [
            dict(x)
            for x in self.connection.execute(
                "SELECT * FROM items WHERE target_id=? ORDER BY item_key", (ident,)
            )
        ]

    def item_counts(self, ident: str) -> tuple[dict, dict]:
        valid, observed = {}, {}
        for row in self.connection.execute(
            """SELECT coalesce(last_valid,'UNKNOWN') AS valid, observed, count(*) AS total
             FROM items WHERE target_id=? GROUP BY valid, observed""",
            (ident,),
        ):
            valid[row["valid"]] = valid.get(row["valid"], 0) + row["total"]
            observed[row["observed"]] = observed.get(row["observed"], 0) + row["total"]
        return valid, observed

    def item_page(self, ident: str, limit: int, offset: int) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM items WHERE target_id=? ORDER BY item_key LIMIT ? OFFSET ?",
                (ident, limit, offset),
            )
        ]

    def platform(self, ident="ticketplus") -> dict:
        return dict(
            self.connection.execute("SELECT * FROM platform WHERE id=?", (ident,)).fetchone()
        )

    def acquire(self, owner: str, now: float, lease_seconds: float) -> dict | None:
        with self.transaction() as db:
            state = dict(db.execute("SELECT * FROM platform WHERE id='ticketplus'").fetchone())
            if state["paused_reason"] or state["blocked_until"] > now or state["lease_until"] > now:
                return state
            db.execute(
                "UPDATE platform SET lease_owner=?,lease_until=? WHERE id='ticketplus'",
                (owner, now + lease_seconds),
            )
        return None

    def release(self, owner: str):
        self.connection.execute(
            """UPDATE platform SET lease_owner=NULL,lease_until=0
         WHERE id='ticketplus' AND lease_owner=?""",
            (owner,),
        )

    def reserve_request(self, owner: str, now: float, gap: float, lease_seconds: float) -> float:
        with self.transaction() as db:
            state = db.execute("SELECT * FROM platform WHERE id='ticketplus'").fetchone()
            if state["lease_owner"] != owner or state["lease_until"] <= now:
                raise RuntimeError("平台查詢租約已失效")
            if state["next_request"] > now:
                return state["next_request"] - now
            db.execute(
                """UPDATE platform SET next_request=?,lease_until=?,request_count=request_count+1
             WHERE id='ticketplus'""",
                (now + gap, now + lease_seconds),
            )
        return 0

    def metadata(self, key: str, now: float) -> dict | None:
        row = self.connection.execute(
            "SELECT payload FROM metadata WHERE key=? AND expires_at>?", (key, now)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_metadata(self, key: str, value: dict, expires_at: float):
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?,?)",
            (key, expires_at, json.dumps(value, ensure_ascii=False)),
        )

    def enqueue(
        self,
        db,
        event_id: str,
        target_id: str | None,
        kind: str,
        now: float,
        payload: dict,
        ttl: float,
        enabled: bool,
        channel_id: str = "default",
    ):
        payload = {**payload, "channel_id": channel_id}
        if target_id:
            target = db.execute("SELECT signature FROM targets WHERE id=?", (target_id,)).fetchone()
            if target:
                payload = {**payload, "target_signature": target["signature"]}
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (event_id, target_id, kind, now, json.dumps(payload, ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO outbox(event_id,status,next_attempt,expires_at) VALUES(?,?,?,?)",
            (event_id, "PENDING" if enabled else "DISABLED", now, now + ttl),
        )

    def cancel_unavailable(self, db, target_id: str, unavailable: set[str], *, kind="RELEASE"):
        if not unavailable:
            return
        rows = db.execute(
            """SELECT o.event_id,o.excluded_items,e.payload FROM outbox o JOIN events e ON e.id=o.event_id
         WHERE e.target_id=? AND e.kind=? AND o.status IN ('PENDING','INFLIGHT')""",
            (target_id, kind),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            excluded = set(json.loads(row["excluded_items"])) | unavailable
            remaining = [x for x in payload["changes"] if x["item_key"] not in excluded]
            db.execute(
                "UPDATE outbox SET excluded_items=? WHERE event_id=?",
                (json.dumps(sorted(excluded)), row["event_id"]),
            )
            if not remaining:
                db.execute(
                    "UPDATE outbox SET status='CANCELLED' WHERE event_id=?", (row["event_id"],)
                )

    def claim_notice(
        self,
        now: float,
        max_attempts: int = 6,
        lease_seconds: float = 120,
        *,
        channels: set[str] | None = None,
        event_id: str | None = None,
    ) -> dict | None:
        with self.transaction() as db:
            db.execute(
                """UPDATE outbox SET status='EXPIRED',lease_until=NULL
             WHERE status IN ('PENDING','INFLIGHT') AND expires_at<=?""",
                (now,),
            )
            db.execute(
                """UPDATE outbox SET status='PENDING',lease_until=NULL
             WHERE status='INFLIGHT' AND lease_until<=?""",
                (now,),
            )
            db.execute(
                "UPDATE outbox SET status='FAILED' WHERE status='PENDING' AND attempts>=?",
                (max_attempts,),
            )
            platform = db.execute("SELECT * FROM platform WHERE id='discord'").fetchone()
            if platform["blocked_until"] > now or platform["lease_until"] > now:
                return None
            filters, params = [], [now]
            if channels is not None:
                if not channels:
                    return None
                filters.append(
                    "coalesce(json_extract(e.payload,'$.channel_id'),'default') IN ("
                    + ",".join("?" for _ in channels)
                    + ")"
                )
                params.extend(sorted(channels))
            if event_id is not None:
                filters.append("o.event_id=?")
                params.append(event_id)
            extra = " AND " + " AND ".join(filters) if filters else ""
            row = db.execute(
                """SELECT o.*,e.target_id,e.kind,e.payload,e.created_at
             FROM outbox o JOIN events e ON e.id=o.event_id
             WHERE o.status='PENDING' AND o.next_attempt<=?"""
                + extra
                + " ORDER BY o.next_attempt,e.created_at LIMIT 1",
                params,
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE outbox SET status='INFLIGHT',lease_until=?,attempts=attempts+1 WHERE event_id=?",
                (now + lease_seconds, row["event_id"]),
            )
            notice = dict(row)
            notice["attempts"] += 1
            # Attempts only increase; a recovered delivery gets a different owner.
            notice["claim_token"] = f"{row['event_id']}:{notice['attempts']}"
            db.execute(
                "UPDATE platform SET lease_owner=?,lease_until=? WHERE id='discord'",
                (notice["claim_token"], now + lease_seconds),
            )
            notice["payload"] = json.loads(notice["payload"])
            return notice

    def finish_notice(
        self,
        notice: dict,
        now: float,
        status: str,
        *,
        message_id=None,
        error=None,
        next_attempt=None,
        blocked_until=0,
    ) -> bool:
        with self.transaction() as db:
            # A real 429 still applies even if this notice was cancelled in flight.
            if blocked_until:
                db.execute(
                    "UPDATE platform SET blocked_until=max(blocked_until,?) WHERE id='discord'",
                    (blocked_until,),
                )
            updated = db.execute(
                """UPDATE outbox SET status=?,message_id=?,last_error=?,
                 next_attempt=coalesce(?,next_attempt),lease_until=NULL
                 WHERE event_id=? AND status='INFLIGHT' AND attempts=? AND lease_until>?
                 AND EXISTS (SELECT 1 FROM platform WHERE id='discord'
                  AND lease_owner=? AND lease_until>?)""",
                (
                    status,
                    message_id,
                    error,
                    next_attempt,
                    notice["event_id"],
                    notice["attempts"],
                    now,
                    notice["claim_token"],
                    now,
                ),
            ).rowcount
            db.execute(
                "UPDATE platform SET lease_owner=NULL,lease_until=0 WHERE id='discord' AND lease_owner=?",
                (notice["claim_token"],),
            )
            return bool(updated)

    def notification_status(self, event_id: str | None) -> dict:
        if event_id is None:
            return {"status": "NOT_REQUIRED"}
        row = self.connection.execute(
            "SELECT status,message_id,attempts,last_error FROM outbox WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else {"status": "NOT_REQUIRED"}

    def event_page(self, target_id: str | None, limit: int, offset: int) -> dict:
        where = "WHERE e.target_id=?" if target_id else ""
        args = (target_id,) if target_id else ()
        total = self.connection.execute(f"SELECT count(*) FROM events e {where}", args).fetchone()[
            0
        ]
        rows = self.connection.execute(
            f"""SELECT e.*,o.status AS notification_status,o.message_id,
         o.attempts,o.last_error FROM events e LEFT JOIN outbox o ON e.id=o.event_id {where}
         ORDER BY e.created_at DESC,e.id LIMIT ? OFFSET ?""",
            (*args, limit, offset),
        ).fetchall()
        return {
            "events": [{**dict(r), "payload": json.loads(r["payload"])} for r in rows],
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def heartbeat(self, now: float):
        self.connection.execute("INSERT OR REPLACE INTO runtime VALUES('heartbeat',?)", (str(now),))

    def prune(self, now: float, days: int):
        def due():
            row = self.connection.execute(
                "SELECT value FROM runtime WHERE key='last_prune'"
            ).fetchone()
            return row is None or now - float(row[0]) >= 3600

        # Avoid a write transaction on every scheduler wake-up. Recheck under the
        # lock so multiple processes share the same hourly maintenance interval.
        if not due():
            return
        cutoff = now - days * 86400
        with self.transaction() as db:
            if not due():
                return
            db.execute(
                """DELETE FROM outbox WHERE status NOT IN ('PENDING','INFLIGHT') AND event_id IN
             (SELECT id FROM events WHERE created_at<?)""",
                (cutoff,),
            )
            db.execute(
                "DELETE FROM events WHERE created_at<? AND id NOT IN (SELECT event_id FROM outbox)",
                (cutoff,),
            )
            db.execute("DELETE FROM metadata WHERE expires_at<?", (now,))
            db.execute("INSERT OR REPLACE INTO runtime VALUES('last_prune',?)", (str(now),))
