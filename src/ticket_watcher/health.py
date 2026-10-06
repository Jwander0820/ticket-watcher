"""Read health without starting a worker, migrating storage or opening HTTP clients."""

import sqlite3
from contextlib import closing

from .config import Config
from .models import Result, timestamp, utcnow


def health_snapshot(connection, targets, now: float) -> Result:
    row = connection.execute("SELECT value FROM runtime WHERE key='heartbeat'").fetchone()
    heartbeat = float(row[0]) if row else None
    pending = connection.execute(
        "SELECT count(*) FROM outbox WHERE status IN ('PENDING','INFLIGHT')"
    ).fetchone()[0]
    platform = connection.execute(
        "SELECT paused_reason FROM platform WHERE id='ticketplus'"
    ).fetchone()
    observations = {
        row[0]: {"last_success": timestamp(row[1]), "last_error": row[2]}
        for row in connection.execute("SELECT id,last_success,last_error FROM targets")
    }
    return Result(
        data={
            "process_healthy": heartbeat is not None and 0 <= now - heartbeat < 120,
            "last_heartbeat": timestamp(heartbeat),
            "platform_paused": platform[0] if platform else None,
            "pending_notifications": pending,
            "observations": [
                {
                    "target_id": target.id,
                    **observations.get(target.id, {"last_success": None, "last_error": None}),
                }
                for target in targets
            ],
        }
    )


def read_health(config: Config, clock=utcnow) -> Result:
    uri = config.database_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1, isolation_level=None)) as connection:
        # One consistent read snapshot; never initialize or migrate a missing/old DB.
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("資料庫版本不支援，請先由監控程序啟動或更新")
        return health_snapshot(connection, config.targets, clock())
