import asyncio
import json
import logging
import random
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager

import httpx

from . import __version__
from .config import Config, Target
from .models import Observation, Result, SourceError, TicketStatus, timestamp, utcnow
from .notifications import DiscordNotifier
from .platforms.ticketplus import TicketPlusAdapter
from .query_log import QueryLog
from .storage import Store
from .transport import PublicTransport

log = logging.getLogger(__name__)
LOCAL_POLL_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 30


def capabilities() -> Result:
    return Result(
        data={
            "version": __version__,
            "operations": [
                "capabilities",
                "query",
                "status",
                "events",
                "check",
                "tick",
                "run",
                "health",
                "resume",
                "ui",
            ],
            "platforms": [
                {
                    "id": "ticketplus",
                    "granularity": "SESSION",
                    "source": "ticketplus-public-v2/session",
                    "requires_login": False,
                    "item_filters_supported": True,
                    "supported_granularities": ["SESSION", "AREA", "PRODUCT"],
                    "url_modes": {"activity": "SESSION", "order": "AREA_OR_PRODUCT"},
                    "release_hint_supported": True,
                }
            ],
            "requires": {
                "python": ">=3.12",
                "shared_state": "SQLite",
                "browser": False,
                "ai": False,
            },
        }
    )


def _page(values: list, limit: int, offset: int) -> dict:
    return {
        "items": values[offset : offset + limit],
        "total": len(values),
        "next_offset": offset + limit if offset + limit < len(values) else None,
    }


def observation_result(observation: Observation, detail: bool, limit: int, offset: int) -> dict:
    data = {
        "event_name": observation.event_name,
        "public_url": observation.public_url,
        "source": observation.source,
        "granularity": observation.granularity,
        "observed_at": timestamp(observation.observed_at),
        "complete": observation.complete,
        "summary": dict(Counter(x.status.value for x in observation.items)),
        "request_count": observation.request_count,
    }
    if detail:
        data["page"] = _page([x.to_dict() for x in observation.items], limit, offset)
    return data


class Watcher:
    """Reusable core. Callers share one database to share state, throttling and outbox."""

    def __init__(self, config: Config, *, client=None, clock=utcnow, rng=None, adapter=None):
        self.config, self.clock = config, clock
        self.rng = rng or random.SystemRandom()
        self.store = Store(config.database_path)
        self.query_log = QueryLog(self.store, config.database_path, clock)
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=config.timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": f"TicketWatcher/{__version__}"},
        )
        self.transport = PublicTransport(self.client, self.store, config, clock)
        self.adapter = adapter or TicketPlusAdapter(self.transport)
        self.notifier = DiscordNotifier(self.client, self.store, config, clock)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self._owns_client:
            await self.client.aclose()
        self.store.close()

    def _target(self, ident: str) -> Target:
        for target in self.config.targets:
            if target.id == ident:
                return target
        raise ValueError("找不到指定的監控目標")

    def _deferred(self, reason: str, allowed: float | None = None) -> Result:
        return Result(
            "DEFERRED",
            data={
                "reason": reason,
                "next_allowed_at": timestamp(allowed),
                "evaluation": {"performed": False, "release_detected": None},
            },
        )

    async def _fetch(self, target: Target):
        owner = str(uuid.uuid4())
        state = self.store.acquire(owner, self.clock(), self.transport.lease_seconds)
        if state:
            if state["paused_reason"]:
                return self._deferred("PLATFORM_PAUSED")
            return self._deferred(
                "PLATFORM_BUSY_OR_BACKOFF", max(state["blocked_until"], state["lease_until"])
            )
        self.transport.owner, self.transport.count = owner, 0
        try:
            observation = await self.adapter.fetch(target)
            if observation.complete:
                # Query shares platform backoff but must not change ticket baselines.
                self.store.connection.execute(
                    "UPDATE platform SET failures=0 WHERE id='ticketplus'"
                )
            return observation
        except SourceError as error:
            return self._error(error)
        finally:
            # State application is protected by the same lease in check(), below.
            self.store.release(owner)

    def _system(self, db, message: str, target_id: str | None = None):
        self.store.enqueue(
            db,
            str(uuid.uuid4()),
            target_id,
            "SYSTEM",
            self.clock(),
            {"message": message},
            self.config.notification_ttl,
            self.config.system_alerts,
            channel_id=self._target(target_id).channel_id if target_id else "default",
        )

    def _error(self, error: SourceError, target: Target | None = None) -> Result:
        now = self.clock()
        with self.store.transaction() as db:
            platform = self.store.platform()
            if error.code == "BLOCKED" and not platform["paused_reason"]:
                db.execute("UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'")
                if target:
                    self._system(
                        db, "TicketPlus 拒絕存取或要求驗證，平台查詢已暫停，需人工處理。", target.id
                    )
            if error.code == "RATE_LIMITED" or (target is None and error.code == "NETWORK"):
                failures = platform["failures"] + 1
                local_delay = self.config.backoff[min(failures - 1, len(self.config.backoff) - 1)]
                blocked_until = now + max(local_delay + self.rng.randint(0, 30), error.retry_after)
                db.execute(
                    "UPDATE platform SET blocked_until=max(blocked_until,?),failures=? WHERE id='ticketplus'",
                    (blocked_until, failures),
                )
            if target:
                old = self.store.target(target.id)
                failures = old["failures"] + 1
                parses = old["parse_failures"] + 1 if error.code == "PARSE" else 0
                pause = (
                    "UNSUPPORTED"
                    if error.code == "UNSUPPORTED"
                    else "PARSE"
                    if parses >= 3
                    else old["paused_reason"]
                )
                delay = self.config.backoff[min(failures - 1, len(self.config.backoff) - 1)]
                next_check = now + max(delay + self.rng.randint(0, 30), error.retry_after)
                db.execute(
                    """UPDATE targets SET failures=?,parse_failures=?,paused_reason=?,last_checked=?,
                 last_error=?,next_check=?,request_count=? WHERE id=?""",
                    (
                        failures,
                        parses,
                        pause,
                        now,
                        error.code,
                        next_check,
                        self.transport.count,
                        target.id,
                    ),
                )
                db.execute(
                    "UPDATE items SET observed='UNKNOWN',observed_at=? WHERE target_id=?",
                    (now, target.id),
                )
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        target.id,
                        "ERROR",
                        now,
                        json.dumps({"code": error.code, "message": str(error)}),
                    ),
                )
                if (failures == 1 and error.code != "BLOCKED") or (
                    pause and not old["paused_reason"]
                ):
                    self._system(
                        db,
                        f"目標 {target.id} 查詢異常：{error.code}"
                        + ("，已暫停。" if pause else "，保留最後有效票況並退避。"),
                        target.id,
                    )
        status = "UNSUPPORTED" if error.code == "UNSUPPORTED" else "FAILED"
        log.warning(
            "source_failure code=%s target=%s requests=%d",
            error.code,
            target.id if target else "query",
            self.transport.count,
        )
        return Result(
            status,
            "LIVE",
            {
                "error": {"code": error.code, "message": str(error)},
                "evaluation": {"performed": False, "release_detected": None},
                "request_count": self.transport.count,
                "next_allowed_at": timestamp(
                    max(
                        self.store.platform()["blocked_until"],
                        self.store.target(target.id)["next_check"] if target else 0,
                    )
                ),
            },
        )

    async def query(
        self, url: str, *, session_ids=(), item_ids=(), detail=False, limit=50, offset=0
    ) -> Result:
        started = time.monotonic()
        result = await self._query(
            url,
            session_ids=session_ids,
            item_ids=item_ids,
            detail=detail,
            limit=limit,
            offset=offset,
        )
        self.query_log.record(None, "query", result, time.monotonic() - started)
        return result

    async def _query(
        self, url: str, *, session_ids=(), item_ids=(), detail=False, limit=50, offset=0
    ) -> Result:
        target = Target(
            "query", "query", url, session_ids=tuple(session_ids), item_ids=tuple(item_ids)
        )
        try:
            observation = await self._fetch(target)
        except SourceError as error:
            return self._error(error)
        if isinstance(observation, Result):
            return observation
        return Result(
            result_source="LIVE",
            data={
                **observation_result(observation, detail, limit, offset),
                "evaluation": {"performed": False, "release_detected": None},
                "notification": {"status": "NOT_APPLICABLE"},
            },
        )

    def _apply(
        self, target: Target, observation: Observation
    ) -> tuple[list[dict], str | None, str | None]:
        now = observation.observed_at
        changes, releases, hints, unavailable, no_hints = [], [], [], set(), set()
        with self.store.transaction() as db:
            old = self.store.target(target.id)
            if old["source"] and old["source"] != observation.source:
                db.execute("DELETE FROM items WHERE target_id=?", (target.id,))
                db.execute(
                    """UPDATE outbox SET status='CANCELLED' WHERE status IN ('PENDING','INFLIGHT') AND event_id IN
                 (SELECT id FROM events WHERE target_id=?)""",
                    (target.id,),
                )
                old = {**old, "mode": "NORMAL", "active_until": None, "no_available": 0}
            existing = {x["item_key"]: x for x in self.store.items(target.id)}
            observed_keys = set()
            for item in observation.items:
                observed_keys.add(item.item_key)
                previous = existing.get(item.item_key)
                valid = item.status != TicketStatus.UNKNOWN
                last_valid = (
                    item.status.value if valid else previous["last_valid"] if previous else None
                )
                valid_at = now if valid else previous["valid_at"] if previous else None
                sequence = previous["release_sequence"] if previous else 0
                if (
                    valid
                    and previous
                    and previous["last_valid"]
                    and previous["last_valid"] != item.status
                ):
                    change = {
                        "item_key": item.item_key,
                        "previous": previous["last_valid"],
                        "current": item.status.value,
                        "previous_observed_at": previous["valid_at"],
                        "observed_at": now,
                        "monitoring_gap": now - previous["valid_at"] > 2700,
                        "item": item.to_dict(),
                    }
                    changes.append(change)
                    if (
                        previous["last_valid"] in {"SOLD_OUT", "TEMPORARILY_UNAVAILABLE"}
                        and item.status == TicketStatus.AVAILABLE
                    ):
                        sequence += 1
                        releases.append({**change, "release_sequence": sequence})
                    elif (
                        previous["last_valid"] == "SOLD_OUT"
                        and item.status == TicketStatus.TEMPORARILY_UNAVAILABLE
                        and observation.granularity == "SESSION"
                    ):
                        hints.append(change)
                if valid and item.status != TicketStatus.AVAILABLE:
                    unavailable.add(item.item_key)
                if valid and item.status != TicketStatus.TEMPORARILY_UNAVAILABLE:
                    no_hints.add(item.item_key)
                db.execute(
                    """INSERT INTO items VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(target_id,item_key)
                 DO UPDATE SET last_valid=excluded.last_valid,valid_at=excluded.valid_at,observed=excluded.observed,
                 observed_at=excluded.observed_at,release_sequence=excluded.release_sequence,details=excluded.details""",
                    (
                        target.id,
                        item.item_key,
                        last_valid,
                        valid_at,
                        item.status.value,
                        now,
                        sequence,
                        json.dumps(item.to_dict(), ensure_ascii=False),
                    ),
                )
            missing = set(existing) - observed_keys
            for key in missing:
                db.execute(
                    "UPDATE items SET observed='UNKNOWN',observed_at=? WHERE target_id=? AND item_key=?",
                    (now, target.id, key),
                )
            complete = observation.complete and not missing
            self.store.cancel_unavailable(db, target.id, unavailable)
            self.store.cancel_unavailable(db, target.id, no_hints, kind="RELEASE_HINT")
            active_until, no_available = old["active_until"], old["no_available"]
            if releases or hints:
                active_until, no_available = now + self.config.active_window, 0
            elif complete:
                no_available = (
                    0
                    if any(x.status == TicketStatus.AVAILABLE for x in observation.items)
                    else no_available + 1
                )
            mode = (
                "ACTIVE"
                if active_until
                and active_until > now
                and no_available < self.config.exit_active_checks
                else "NORMAL"
            )
            if mode == "NORMAL":
                active_until = None
            parses = 0 if complete else old["parse_failures"] + 1
            pause = "PARSE" if parses >= 3 else None
            interval = (
                self.config.active_interval if mode == "ACTIVE" else self.config.normal_interval
            )
            next_check = self.clock() + self.rng.randint(*interval)
            if not complete:
                next_check = max(
                    next_check,
                    self.clock()
                    + self.config.backoff[min(parses - 1, len(self.config.backoff) - 1)],
                )
            db.execute(
                """UPDATE targets SET mode=?,active_until=?,no_available=?,failures=?,parse_failures=?,
             paused_reason=?,last_checked=?,last_success=?,last_error=?,next_check=?,source=?,event_name=?,request_count=? WHERE id=?""",
                (
                    mode,
                    active_until,
                    no_available,
                    0 if complete else old["failures"] + 1,
                    parses,
                    pause,
                    now,
                    now if complete else old["last_success"],
                    None if complete else "PARTIAL_DATA",
                    next_check,
                    observation.source,
                    observation.event_name,
                    observation.request_count,
                    target.id,
                ),
            )
            if complete:
                db.execute("UPDATE platform SET failures=0 WHERE id='ticketplus'")
            if complete and old["last_error"]:
                self._system(db, f"目標 {target.id} 已恢復取得完整有效票況。", target.id)
            if not complete and (old["parse_failures"] == 0 or pause):
                self._system(
                    db,
                    f"目標 {target.id} 票況資料不完整"
                    + ("，已暫停。" if pause else "，保留缺失項目的最後有效票況。"),
                    target.id,
                )
            notified_keys = {x["item_key"] for x in releases + hints}
            non_releases = [x for x in changes if x["item_key"] not in notified_keys]
            if non_releases:
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        target.id,
                        "STATE_CHANGE",
                        now,
                        json.dumps({"changes": non_releases}, ensure_ascii=False),
                    ),
                )
            event_id = None
            if releases:
                event_id = str(uuid.uuid4())
                self.store.enqueue(
                    db,
                    event_id,
                    target.id,
                    "RELEASE",
                    now,
                    {
                        "event_name": observation.event_name,
                        "public_url": observation.public_url,
                        "granularity": observation.granularity,
                        "changes": releases,
                    },
                    self.config.notification_ttl,
                    True,
                    channel_id=target.channel_id,
                )
            hint_event_id = None
            if hints:
                hint_event_id = str(uuid.uuid4())
                self.store.enqueue(
                    db,
                    hint_event_id,
                    target.id,
                    "RELEASE_HINT",
                    now,
                    {
                        "event_name": observation.event_name,
                        "public_url": observation.public_url,
                        "granularity": "SESSION",
                        "changes": hints,
                        "signal": "暫無票券",
                    },
                    self.config.notification_ttl,
                    True,
                    channel_id=target.channel_id,
                )
        return changes, event_id, hint_event_id

    async def check(
        self, ident: str, *, detail=False, limit=50, offset=0, immediate=False
    ) -> Result:
        result = await self._check(
            ident,
            detail=detail,
            limit=limit,
            offset=offset,
            immediate=immediate,
            mode="manual" if immediate else "check",
        )
        if result.execution_status != "DEFERRED":
            await self.notifier.deliver()
        return self._notification_result(result)

    def _notification_result(self, result: Result) -> Result:
        if result.execution_status == "COMPLETED":
            result.data["notification"] = self.store.notification_status(result.data["event_id"])
            result.data["hint_notification"] = self.store.notification_status(
                result.data["hint_event_id"]
            )
        return result

    async def _check(
        self, ident: str, *, detail=False, limit=50, offset=0, immediate=False, mode="scheduled"
    ) -> Result:
        self._target(ident)
        started = time.monotonic()
        try:
            result = await self._perform_check(
                ident, detail=detail, limit=limit, offset=offset, immediate=immediate
            )
        except BaseException as error:
            result = Result(
                "INTERRUPTED" if isinstance(error, asyncio.CancelledError) else "FAILED",
                data={
                    "error": {
                        "code": "CANCELLED"
                        if isinstance(error, asyncio.CancelledError)
                        else "INTERNAL_ERROR"
                    }
                },
            )
            self.query_log.record(ident, mode, result, time.monotonic() - started)
            raise
        # Do not record the scheduler's repeated five-second cooldown checks.
        if result.execution_status != "DEFERRED" or mode != "scheduled":
            self.query_log.record(ident, mode, result, time.monotonic() - started)
        return result

    async def _perform_check(
        self, ident: str, *, detail=False, limit=50, offset=0, immediate=False
    ) -> Result:
        target = self._target(ident)
        if not target.enabled or (target.stop_at is not None and target.stop_at <= self.clock()):
            return self._deferred("TARGET_DISABLED_OR_STOPPED")
        owner = str(uuid.uuid4())
        state = self.store.acquire(owner, self.clock(), self.transport.lease_seconds)
        if state:
            return self._deferred(
                "PLATFORM_PAUSED" if state["paused_reason"] else "PLATFORM_BUSY_OR_BACKOFF",
                None
                if state["paused_reason"]
                else max(state["blocked_until"], state["lease_until"]),
            )
        self.transport.owner, self.transport.count = owner, 0
        try:
            self.store.sync_target(target)
            schedule = self.store.target(ident)
            if schedule["paused_reason"]:
                return self._deferred("TARGET_PAUSED")
            if schedule["next_check"] > self.clock():
                if not immediate or schedule["last_error"]:
                    return self._deferred(
                        "TARGET_BACKOFF" if immediate else "NOT_DUE", schedule["next_check"]
                    )
            try:
                observation = await self.adapter.fetch(target)
            except SourceError as error:
                result = self._error(error, target)
            else:
                changes, event_id, hint_event_id = self._apply(target, observation)
                result = Result(
                    result_source="LIVE",
                    data={
                        **observation_result(observation, detail, limit, offset),
                        "target_id": ident,
                        "evaluation": {
                            "performed": True,
                            "release_detected": bool(event_id),
                            "release_hint_detected": bool(hint_event_id),
                        },
                        "changes": changes[offset : offset + limit],
                        "changes_total": len(changes),
                        "changes_next_offset": offset + limit
                        if offset + limit < len(changes)
                        else None,
                        "event_id": event_id,
                        "hint_event_id": hint_event_id,
                        "complete": self.store.target(ident)["last_error"] is None,
                        "next_allowed_at": timestamp(self.store.target(ident)["next_check"]),
                    },
                )
                log.info(
                    "checked target=%s complete=%s releases=%s requests=%d",
                    ident,
                    observation.complete,
                    bool(event_id),
                    observation.request_count,
                )
        finally:
            self.store.release(owner)
        return result

    def status(self, ident=None, *, detail=False, limit=50, offset=0) -> Result:
        if ident:
            self._target(ident)
        ids = [ident] if ident else [x.id for x in self.config.targets]
        targets = []
        for key in ids:
            state = self.store.target(key)
            if not state:
                targets.append({"target_id": key, "state": "NOT_CHECKED"})
                continue
            summary, current = self.store.item_counts(key)
            result = {
                "target_id": key,
                "event_name": state["event_name"],
                "mode": state["mode"],
                "next_allowed_at": timestamp(state["next_check"]),
                "active_until": timestamp(state["active_until"]),
                "last_checked_at": timestamp(state["last_checked"]),
                "observed_at": timestamp(state["last_success"]),
                "age_seconds": max(0, self.clock() - state["last_success"])
                if state["last_success"]
                else None,
                "paused_reason": state["paused_reason"],
                "last_error": state["last_error"],
                "summary": summary,
                "current_observation": current,
            }
            if detail:
                total = sum(summary.values())
                result["page"] = {
                    "items": [
                        {
                            **json.loads(x["details"]),
                            "status": x["observed"],
                            "last_valid_status": x["last_valid"],
                            "last_valid_at": timestamp(x["valid_at"]),
                            "observed_at": timestamp(x["observed_at"]),
                        }
                        for x in self.store.item_page(key, limit, offset)
                    ],
                    "total": total,
                    "next_offset": offset + limit if offset + limit < total else None,
                }
            targets.append(result)
        platform = self.store.platform()
        return Result(
            result_source="CACHE",
            data={
                "targets": targets,
                "platform": {
                    "paused_reason": platform["paused_reason"],
                    "blocked_until": timestamp(platform["blocked_until"]),
                    "request_count": platform["request_count"],
                },
                "evaluation": {"performed": False, "release_detected": None},
            },
        )

    def events(self, ident=None, *, detail=False, limit=50, offset=0) -> Result:
        page = self.store.event_page(ident, limit, offset)
        for event in page["events"]:
            event["created_at"] = timestamp(event["created_at"])
            if not detail:
                payload = event["payload"]
                event["payload"] = {
                    key: value for key, value in payload.items() if key != "changes"
                }
                if "changes" in payload:
                    event["payload"]["changes_total"] = len(payload["changes"])
        return Result(result_source="CACHE", data=page)

    async def tick(self) -> Result:
        wakeup, stop = asyncio.Event(), asyncio.Event()
        # A one-shot round waits for delivery at the end, never between targets.
        async with self._heartbeat(), asyncio.TaskGroup() as tasks:
            delivery = tasks.create_task(self._delivery_loop(wakeup, stop))
            results = await self._check_due(wakeup)
            stop.set()
            wakeup.set()
        return Result(
            data={
                "checks": [self._notification_result(result).to_dict() for result in results],
                "delivery": delivery.result(),
                "health": self.health().data,
            }
        )

    async def _check_due(self, wakeup: asyncio.Event) -> list[Result]:
        results = []
        enabled_ids = {
            t.id
            for t in self.config.targets
            if t.enabled and (t.stop_at is None or t.stop_at > self.clock())
        }
        with self.store.transaction() as db:
            for row in db.execute(
                """SELECT DISTINCT e.target_id FROM outbox o JOIN events e ON e.id=o.event_id
                 WHERE o.status IN ('PENDING','INFLIGHT') AND e.target_id IS NOT NULL"""
            ).fetchall():
                if row[0] not in enabled_ids:
                    db.execute(
                        "UPDATE outbox SET status='CANCELLED' WHERE status IN ('PENDING','INFLIGHT') AND event_id IN (SELECT id FROM events WHERE target_id=?)",
                        (row[0],),
                    )
        for target in self.config.targets:
            if target.id not in enabled_ids:
                continue
            state = self.store.target(target.id)
            if (
                state
                and state["signature"] == target.signature
                and (state["paused_reason"] or state["next_check"] > self.clock())
            ):
                continue
            results.append(await self._check(target.id))
            wakeup.set()
            await asyncio.sleep(0)
        self.store.prune(self.clock(), self.config.retention_days)
        return results

    async def _delivery_loop(self, wakeup: asyncio.Event, stop: asyncio.Event) -> dict:
        sent = 0
        while True:
            # Capture before delivery: if checks finish during a request, perform
            # one final pass for events enqueued while that request was in flight.
            stopping = stop.is_set()
            wakeup.clear()
            result = await self.notifier.deliver()
            sent += result["sent"]
            if stopping:
                return {**result, "sent": sent}
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=LOCAL_POLL_SECONDS)
            except TimeoutError:
                pass

    @asynccontextmanager
    async def _heartbeat(self):
        stop = asyncio.Event()
        self.store.heartbeat(self.clock())
        self.query_log.maintain()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._heartbeat_loop(stop))
            try:
                yield
            finally:
                stop.set()

    async def _heartbeat_loop(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except TimeoutError:
                self.store.heartbeat(self.clock())
                self.query_log.maintain()

    def health(self) -> Result:
        row = self.store.connection.execute(
            "SELECT value FROM runtime WHERE key='heartbeat'"
        ).fetchone()
        heartbeat = float(row[0]) if row else None
        healthy = heartbeat is not None and self.clock() - heartbeat < 120
        pending = self.store.connection.execute(
            "SELECT count(*) FROM outbox WHERE status IN ('PENDING','INFLIGHT')"
        ).fetchone()[0]
        return Result(
            data={
                "process_healthy": healthy,
                "last_heartbeat": timestamp(heartbeat),
                "platform_paused": self.store.platform()["paused_reason"],
                "pending_notifications": pending,
                "observations": [
                    {
                        "target_id": t.id,
                        "last_success": timestamp(
                            (self.store.target(t.id) or {}).get("last_success")
                        ),
                        "last_error": (self.store.target(t.id) or {}).get("last_error"),
                    }
                    for t in self.config.targets
                ],
            }
        )

    def resume(self, ident=None, *, platform=False) -> Result:
        # Explicit manual intervention clears pauses, but never clears server cooldowns.
        with self.store.transaction() as db:
            if platform:
                db.execute("UPDATE platform SET paused_reason=NULL WHERE id='ticketplus'")
            else:
                self._target(ident)
                db.execute(
                    "UPDATE targets SET paused_reason=NULL,parse_failures=0 WHERE id=?", (ident,)
                )
        return Result(
            data={"resumed": "ticketplus" if platform else ident, "cooldowns_preserved": True}
        )

    async def run(self):
        wakeup, stop = asyncio.Event(), asyncio.Event()
        async with self._heartbeat(), asyncio.TaskGroup() as tasks:
            tasks.create_task(self._delivery_loop(wakeup, stop))
            tasks.create_task(self._poll_loop(wakeup))

    async def _poll_loop(self, wakeup: asyncio.Event):
        while True:
            await self._check_due(wakeup)
            await asyncio.sleep(LOCAL_POLL_SECONDS)
