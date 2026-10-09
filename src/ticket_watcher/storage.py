import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import Config, Target
from .schedule import stop_info

SCHEMA_VERSION = 3
OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
 event_id TEXT NOT NULL REFERENCES events(id), channel_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, expires_at REAL NOT NULL,
 lease_until REAL, message_id TEXT, last_error TEXT,
 excluded_items TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(event_id, channel_id)
);
"""

SCHEMA = (
    """
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
CREATE TABLE IF NOT EXISTS target_schedules (
 target_id TEXT PRIMARY KEY REFERENCES targets(id) ON DELETE CASCADE,
 sessions TEXT NOT NULL, stop_at REAL
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, target_id TEXT, kind TEXT NOT NULL,
 created_at REAL NOT NULL, payload TEXT NOT NULL
);
"""
    + OUTBOX_SCHEMA
    + """
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
"""
)


class LeaseLost(Exception):
    """A superseded query must not write results or error state."""


class PlatformRestricted(Exception):
    """A shared restriction stops further requests without counting a new failure."""

    def __init__(self, reason: str, next_allowed_at: float | None = None):
        super().__init__(reason)
        self.reason, self.next_allowed_at = reason, next_allowed_at


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            self.connection.close()
            raise ValueError("資料庫版本不支援")
        if version in (1, 2):
            with self.transaction() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
                if "excluded_items" not in columns:
                    db.execute(
                        "ALTER TABLE outbox ADD COLUMN excluded_items TEXT NOT NULL DEFAULT '[]'"
                    )
                if "channel_id" not in columns:
                    db.execute("ALTER TABLE outbox RENAME TO outbox_legacy")
                    db.execute(OUTBOX_SCHEMA)
                    db.execute(
                        """INSERT INTO outbox SELECT o.event_id,
                         coalesce(json_extract(e.payload,'$.channel_id'),'default'),
                         o.status,o.attempts,o.next_attempt,o.expires_at,o.lease_until,
                         o.message_id,o.last_error,o.excluded_items
                         FROM outbox_legacy o JOIN events e ON e.id=o.event_id"""
                    )
                    db.execute("DROP TABLE outbox_legacy")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.connection.executescript(SCHEMA)
        if version == 0:
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self):
        self.connection.close()

    def data_version(self) -> int:
        # Changes made by this connection do not advance its data_version.
        return self.connection.execute("PRAGMA data_version").fetchone()[0]

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

    def target_schedule(self, target: Target) -> dict | None:
        row = self.connection.execute(
            """SELECT s.sessions,s.stop_at FROM target_schedules s
             JOIN targets t ON t.id=s.target_id WHERE t.id=? AND t.signature=?""",
            (target.id, target.signature),
        ).fetchone()
        return {"sessions": json.loads(row["sessions"]), "stop_at": row["stop_at"]} if row else None

    def save_schedule(self, db, target_id: str, sessions: dict):
        # Stop the whole target only when every selected session has a reliable time.
        deadline = max(sessions.values()) if sessions and None not in sessions.values() else None
        db.execute(
            """INSERT INTO target_schedules VALUES(?,?,?) ON CONFLICT(target_id)
             DO UPDATE SET sessions=excluded.sessions,stop_at=excluded.stop_at""",
            (target_id, json.dumps(sessions), deadline),
        )

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

    def require_lease(self, owner: str, now: float):
        state = self.platform()
        if state["lease_owner"] != owner or state["lease_until"] <= now:
            raise LeaseLost

    def reserve_request(self, owner: str, now: float, gap: float, lease_seconds: float) -> float:
        with self.transaction() as db:
            state = db.execute("SELECT * FROM platform WHERE id='ticketplus'").fetchone()
            if state["lease_owner"] != owner or state["lease_until"] <= now:
                raise LeaseLost
            if state["paused_reason"]:
                raise PlatformRestricted("PLATFORM_PAUSED")
            if state["blocked_until"] > now:
                raise PlatformRestricted("PLATFORM_BUSY_OR_BACKOFF", state["blocked_until"])
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

    def cache_metadata(self, key: str, value: dict, expires_at: float, *, owner=None, clock=None):
        with self.transaction() as db:
            if owner is not None:
                self.require_lease(owner, clock())
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES(?,?,?)",
                (key, expires_at, json.dumps(value, ensure_ascii=False)),
            )

    def discard_metadata(self, key: str, *, owner, clock):
        with self.transaction() as db:
            if owner is not None:
                self.require_lease(owner, clock())
            db.execute("DELETE FROM metadata WHERE key=?", (key,))

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
        *,
        channel_ids: tuple[str, ...] | None = None,
    ):
        destinations = tuple(
            dict.fromkeys(channel_ids if channel_ids is not None else (channel_id,))
        )
        if not destinations:
            raise ValueError("通知需至少一個目的頻道")
        payload = {**payload, "channel_id": destinations[0], "channel_ids": list(destinations)}
        if target_id:
            target = db.execute("SELECT signature FROM targets WHERE id=?", (target_id,)).fetchone()
            if target:
                payload = {**payload, "target_signature": target["signature"]}
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (event_id, target_id, kind, now, json.dumps(payload, ensure_ascii=False)),
        )
        db.executemany(
            "INSERT INTO outbox(event_id,channel_id,status,next_attempt,expires_at) VALUES(?,?,?,?,?)",
            [
                (event_id, channel, "PENDING" if enabled else "DISABLED", now, now + ttl)
                for channel in destinations
            ],
        )

    def cancel_unavailable(self, db, target_id: str, unavailable: set[str], *, kind="RELEASE"):
        if not unavailable:
            return
        rows = db.execute(
            """SELECT o.event_id,o.channel_id,o.excluded_items,e.payload
             FROM outbox o JOIN events e ON e.id=o.event_id
          WHERE e.target_id=? AND e.kind IN ('RELEASE','RELEASE_HINT')
          AND o.status IN ('PENDING','INFLIGHT')""",
            (target_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            expected = "AVAILABLE" if kind == "RELEASE" else "TEMPORARILY_UNAVAILABLE"
            candidates = {
                change["item_key"] for change in payload["changes"] if change["current"] == expected
            } | {
                item["item_key"]
                for item in payload.get("snapshot", [])
                if item["status"] == expected
            }
            excluded = set(json.loads(row["excluded_items"])) | (unavailable & candidates)
            remaining = [x for x in payload["changes"] if x["item_key"] not in excluded]
            db.execute(
                "UPDATE outbox SET excluded_items=? WHERE event_id=? AND channel_id=?",
                (json.dumps(sorted(excluded)), row["event_id"], row["channel_id"]),
            )
            if not remaining:
                db.execute(
                    "UPDATE outbox SET status='CANCELLED' WHERE event_id=? AND channel_id=?",
                    (row["event_id"], row["channel_id"]),
                )

    def cancel_obsolete_notices(self, config: Config, now: float):
        # Cancellation must not depend on Discord's cooldown, delivery lease, or
        # whether a webhook is configured. Once cancelled, work stays cancelled.
        with self.transaction() as db:
            targets = {
                target.id: target
                for target in config.targets
                if target.enabled
                and not stop_info(target, self.target_schedule(target), now)["stop_reason"]
            }
            channels = {"default", *(channel.id for channel in config.channels)}
            rows = db.execute(
                """SELECT e.id,e.target_id,e.payload,o.channel_id
                 FROM events e JOIN outbox o ON o.event_id=e.id
                 WHERE o.status IN ('PENDING','INFLIGHT')"""
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                channel = row["channel_id"]
                obsolete = channel not in channels
                if row["target_id"]:
                    target = targets.get(row["target_id"])
                    obsolete = (
                        obsolete
                        or not target
                        or (
                            channel not in target.notification_channels
                            or payload.get("target_signature", target.signature) != target.signature
                        )
                    )
                elif payload.get("worker_alert"):
                    obsolete = (
                        obsolete
                        or not config.worker_alerts
                        or channel not in config.worker_notification_channels
                    )
                if obsolete:
                    db.execute(
                        "UPDATE outbox SET status='CANCELLED' WHERE event_id=? AND channel_id=?",
                        (row["id"], channel),
                    )

    def next_notice_at(self, now: float, channels: set[str]) -> float | None:
        # Unconfigured notices still need expiration, but must not cause an
        # immediate-delivery loop. In-flight work waits for its actual lease.
        expiry = self.connection.execute(
            "SELECT min(expires_at) FROM outbox WHERE status IN ('PENDING','INFLIGHT')"
        ).fetchone()[0]
        if not channels:
            return expiry
        platform = self.platform("discord")
        due = self.connection.execute(
            """SELECT min(max(o.next_attempt,
                CASE WHEN o.status='INFLIGHT' THEN coalesce(o.lease_until,0) ELSE 0 END,
                ?,?)) FROM outbox o JOIN events e ON e.id=o.event_id
                WHERE o.status IN ('PENDING','INFLIGHT')
                AND o.channel_id IN ("""
            + ",".join("?" for _ in channels)
            + ")",
            (platform["blocked_until"], platform["lease_until"], *sorted(channels)),
        ).fetchone()[0]
        values = [value for value in (expiry, due) if value is not None]
        return max(now, min(values)) if values else None

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
                filters.append("o.channel_id IN (" + ",".join("?" for _ in channels) + ")")
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
                + " ORDER BY o.next_attempt,e.created_at,"
                "coalesce(json_extract(e.payload,'$.notification_order'),0),o.event_id,o.channel_id LIMIT 1",
                params,
            ).fetchone()
            if not row:
                return None
            db.execute(
                """UPDATE outbox SET status='INFLIGHT',lease_until=?,attempts=attempts+1
                 WHERE event_id=? AND channel_id=?""",
                (now + lease_seconds, row["event_id"], row["channel_id"]),
            )
            notice = dict(row)
            notice["attempts"] += 1
            # Attempts only increase; a recovered delivery gets a different owner.
            notice["claim_token"] = f"{row['event_id']}:{row['channel_id']}:{notice['attempts']}"
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
                 WHERE event_id=? AND channel_id=? AND status='INFLIGHT' AND attempts=? AND lease_until>?
                 AND EXISTS (SELECT 1 FROM platform WHERE id='discord'
                  AND lease_owner=? AND lease_until>?)""",
                (
                    status,
                    message_id,
                    error,
                    next_attempt,
                    notice["event_id"],
                    notice["channel_id"],
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

    def notification_status(self, event_id: str | list[str] | None) -> dict:
        ids = list(dict.fromkeys(event_id)) if isinstance(event_id, list) else [event_id]
        if not ids or ids == [None]:
            return {"status": "NOT_REQUIRED"}
        rows = self.connection.execute(
            "SELECT event_id,channel_id,status,message_id,attempts,last_error FROM outbox "
            "WHERE event_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY channel_id,event_id",
            ids,
        ).fetchall()
        if not rows:
            return {"status": "NOT_REQUIRED"}
        deliveries = [dict(row) for row in rows]
        statuses = {row["status"] for row in rows}
        if len(statuses) == 1:
            status = rows[0]["status"]
        else:
            status = next(
                (
                    value
                    for value in (
                        "INFLIGHT",
                        "PENDING",
                        "PARTIAL",
                        "FAILED",
                        "EXPIRED",
                        "CANCELLED",
                    )
                    if (value == "PARTIAL" and "SENT" in statuses) or value in statuses
                ),
                "DISABLED",
            )
        return {
            "status": status,
            "message_id": rows[0]["message_id"] if len(rows) == 1 else None,
            "attempts": sum(row["attempts"] for row in rows),
            "last_error": next((row["last_error"] for row in rows if row["last_error"]), None),
            "deliveries": deliveries,
        }

    def event_page(self, target_id: str | None, limit: int, offset: int) -> dict:
        where = "WHERE e.target_id=?" if target_id else ""
        args = (target_id,) if target_id else ()
        total = self.connection.execute(f"SELECT count(*) FROM events e {where}", args).fetchone()[
            0
        ]
        rows = self.connection.execute(
            f"""SELECT e.* FROM events e {where}
         ORDER BY e.created_at DESC,e.id LIMIT ? OFFSET ?""",
            (*args, limit, offset),
        ).fetchall()
        events = []
        for row in rows:
            notice = self.notification_status(row["id"])
            events.append(
                {
                    **dict(row),
                    "payload": json.loads(row["payload"]),
                    "notification_status": notice["status"] if "deliveries" in notice else None,
                    "message_id": notice.get("message_id"),
                    "attempts": notice.get("attempts"),
                    "last_error": notice.get("last_error"),
                    "deliveries": notice.get("deliveries", []),
                }
            )
        return {
            "events": events,
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def heartbeat(self, now: float):
        self.connection.execute("INSERT OR REPLACE INTO runtime VALUES('heartbeat',?)", (str(now),))

    def next_prune_at(self, now: float) -> float:
        row = self.connection.execute("SELECT value FROM runtime WHERE key='last_prune'").fetchone()
        return float(row[0]) + 3600 if row else now

    def prune(self, now: float, days: int):
        def due():
            return self.next_prune_at(now) <= now

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
             (SELECT id FROM events WHERE created_at<? AND NOT EXISTS
              (SELECT 1 FROM outbox active WHERE active.event_id=events.id
               AND active.status IN ('PENDING','INFLIGHT')))""",
                (cutoff,),
            )
            db.execute(
                "DELETE FROM events WHERE created_at<? AND id NOT IN (SELECT event_id FROM outbox)",
                (cutoff,),
            )
            db.execute("DELETE FROM metadata WHERE expires_at<?", (now,))
            db.execute("INSERT OR REPLACE INTO runtime VALUES('last_prune',?)", (str(now),))
