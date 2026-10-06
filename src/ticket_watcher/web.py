"""Loopback-only control panel. The UI process owns its monitoring worker."""

import asyncio
import copy
import hashlib
import json
import logging
import secrets
import uuid
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from aiohttp import web

from .config import parse_config
from .models import utcnow
from .notifications import channel_urls, validate_webhook, webhook_url
from .private_io import atomic_text, read_webhooks
from .service import Watcher

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
WORKER_RETRY_SECONDS = (5, 15, 30, 60, 300)
WORKER_STABLE_SECONDS = 60
WORKER_FAILURE_MESSAGE = "監控服務意外中止，正在自動重試；重試間隔依序為 5、15、30、60 秒，之後每 5 分鐘一次。穩定恢復後會另行通知。"
SETTINGS = {
    "polling": {
        "normal_interval_seconds",
        "active_interval_seconds",
        "active_window_seconds",
        "exit_active_after_no_available_checks",
    },
    "http": {"min_request_gap_seconds", "timeout_seconds"},
    "notifications": {
        "system_alerts_enabled",
        "delivery_ttl_seconds",
        "worker_alerts_enabled",
        "worker_alert_channel_id",
    },
}


class Conflict(ValueError):
    pass


class Controller:
    def __init__(self, path: Path, *, watcher_factory=Watcher, monitor=True):
        self.path = path.resolve()
        self.factory, self.monitor = watcher_factory, monitor
        self.lock = asyncio.Lock()
        self.csrf = secrets.token_urlsafe(32)
        self.task = None
        self.watcher = None
        self.runner_error = None
        self.restart_count = 0
        self.retry_at = None
        self.actions = set()

    async def start(self):
        if not self.path.exists():
            atomic_text(self.path, "app:\n  database_path: watcher.db\ntargets: []\nchannels: []\n")
        self.document = yaml.safe_load(self.path.read_text(encoding="utf-8-sig")) or {}
        self.config = parse_config(self.document, self.path.parent)
        channel_urls(self.config)  # Validate credentials without connecting to Discord.
        self.revision = self.disk_revision()
        self._start_worker()

    def disk_revision(self):
        credential = self.config.secrets_path
        content = self.path.read_bytes()
        if credential.exists():
            content += b"\0" + credential.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def _start_worker(self):
        self.runner_error = None
        startup_error = None
        try:
            self.watcher = self.factory(self.config)
        except Exception as error:
            if not self.monitor:
                raise
            startup_error = error
            self.runner_error = "監控啟動失敗，正在自動重試。"
        if self.monitor:
            self.task = asyncio.create_task(self._run(startup_error))

    async def _run(self, startup_error=None):
        failures = 0
        incident = None
        while True:
            try:
                if startup_error is not None:
                    error, startup_error = startup_error, None
                    raise error
                if failures or self.watcher is None:
                    # Serialize replacement with manual queries and settings saves.
                    # Keep the old instance readable if constructing a replacement fails.
                    async with self.lock:
                        await self._stop_actions()
                        replacement = self.factory(self.config)
                        old, self.watcher = self.watcher, replacement
                        if old is not None:
                            cleanup = asyncio.create_task(old.__aexit__(None, None, None))
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                with suppress(Exception):
                                    await cleanup
                                raise
                            except Exception as error:
                                log.error("ui_worker_cleanup_failed type=%s", type(error).__name__)
                    self.restart_count += 1
                    self.retry_at = None
                    self.runner_error = "監控已重新啟動，正在確認穩定運作。"
                    self._worker_event(incident + "-failed", WORKER_FAILURE_MESSAGE)
                worker = asyncio.create_task(self.watcher.run())
                try:
                    done, _ = await asyncio.wait({worker}, timeout=WORKER_STABLE_SECONDS)
                    if not done and incident:
                        self.runner_error = None
                        self._worker_event(
                            incident + "-recovered",
                            "監控服務已恢復，並持續運作至少 60 秒。原有票況基準、等待期限與平台暫停狀態均保留。",
                        )
                        incident, failures = None, 0
                    await worker
                    raise RuntimeError("worker_returned")
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    raise RuntimeError("worker_cancelled") from None
                finally:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = WORKER_RETRY_SECONDS[min(failures, len(WORKER_RETRY_SECONDS) - 1)]
                failures += 1
                incident = incident or str(uuid.uuid4())
                self.retry_at = (self.watcher.clock() if self.watcher else utcnow()) + delay
                self.runner_error = (
                    f"監控意外中止，將於 {delay} 秒後自動重試（連續異常 {failures} 次）。"
                )
                log.error("ui_worker_failed type=%s retry_seconds=%s", type(error).__name__, delay)
                try:
                    self.watcher.store.connection.execute(
                        "DELETE FROM runtime WHERE key='heartbeat'"
                    )
                except Exception:
                    pass
                event_id = incident + "-failed"
                self._worker_event(event_id, WORKER_FAILURE_MESSAGE)
                # Delivery has its own time budget and must never prevent recovery.
                # Sleep concurrently so a healthy Discord does not extend backoff.
                await asyncio.gather(
                    asyncio.sleep(delay), self._deliver_worker_event(event_id, min(delay, 10))
                )

    def _worker_event(self, event_id, message):
        if self.watcher is None:
            return
        try:
            with self.watcher.store.transaction() as db:
                if not db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
                    self.watcher.store.enqueue(
                        db,
                        event_id,
                        None,
                        "SYSTEM",
                        self.watcher.clock(),
                        {"message": message, "worker_alert": True},
                        self.config.notification_ttl,
                        self.config.worker_alerts,
                        channel_id=self.config.worker_alert_channel,
                    )
        except Exception as error:
            log.error("ui_worker_event_failed type=%s", type(error).__name__)

    async def _deliver_worker_event(self, event_id, timeout):
        if self.watcher is None:
            return
        try:
            async with asyncio.timeout(timeout):
                await self.watcher.notifier.deliver(max_messages=1, event_id=event_id)
        except Exception as error:
            log.error("ui_worker_alert_failed type=%s", type(error).__name__)

    def begin_action(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.actions.add(task)
        task.add_done_callback(self.actions.discard)
        return task

    async def _stop_actions(self):
        tasks = list(self.actions)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        await self._stop_actions()
        if self.watcher:
            await self.watcher.__aexit__(None, None, None)
            self.watcher = None
        self.retry_at = None

    def require_revision(self, value):
        if value != self.revision or self.disk_revision() != self.revision:
            raise Conflict(
                "設定已在其他視窗或檔案中變更。請重新整理；若曾手動編輯檔案，請重啟 UI。"
            )

    async def save(self, document: dict, credentials: dict):
        config = parse_config(document, self.path.parent)
        for value in credentials.values():
            validate_webhook(value)
        if not credentials.get("default"):
            webhook_url(config.webhook_url_env)
        old = read_webhooks(self.config.secrets_path)
        await self.close()
        try:
            atomic_text(config.secrets_path, json.dumps(credentials, ensure_ascii=False))
            atomic_text(self.path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
        except OSError:
            atomic_text(self.config.secrets_path, json.dumps(old, ensure_ascii=False))
            self._start_worker()
            raise
        self.config, self.document = config, document
        self.revision = self.disk_revision()
        self._start_worker()

    def settings(self):
        c = self.config
        return {
            "polling": {
                "normal_interval_seconds": list(c.normal_interval),
                "active_interval_seconds": list(c.active_interval),
                "active_window_seconds": c.active_window,
                "exit_active_after_no_available_checks": c.exit_active_checks,
            },
            "http": {"min_request_gap_seconds": c.request_gap, "timeout_seconds": c.timeout},
            "notifications": {
                "system_alerts_enabled": c.system_alerts,
                "worker_alerts_enabled": c.worker_alerts,
                "worker_alert_channel_id": c.worker_alert_channel,
                "delivery_ttl_seconds": c.notification_ttl,
            },
        }

    def state(self):
        states = (
            {s["target_id"]: s for s in self.watcher.status().data["targets"]}
            if self.watcher
            else {}
        )
        targets = []
        for target in self.config.targets:
            value = asdict(target)
            value["stop_at"] = (
                datetime.fromtimestamp(target.stop_at, UTC).isoformat() if target.stop_at else None
            )
            targets.append({**value, "state": states.get(target.id, {})})
        configured = set(channel_urls(self.config))
        query_logs = (
            {**self.watcher.query_log.recent(), "error": self.watcher.query_log.error}
            if self.watcher
            else {"entries": [], "cycle_started_at": None, "error": None}
        )
        return {
            "csrf": self.csrf,
            "revision": self.revision,
            "settings": self.settings(),
            "targets": targets,
            "channels": [
                {
                    "id": "default",
                    "name": "預設頻道",
                    "configured": "default" in configured,
                    "readonly": False,
                    "source": "ui"
                    if read_webhooks(self.config.secrets_path).get("default")
                    else "environment",
                },
                *[
                    {
                        "id": c.id,
                        "name": c.name,
                        "configured": c.id in configured,
                        "readonly": False,
                    }
                    for c in self.config.channels
                ],
            ],
            "health": self.watcher.health().data
            if self.watcher
            else {
                "process_healthy": False,
                "last_heartbeat": None,
                "platform_paused": None,
                "pending_notifications": None,
                "observations": [],
            },
            "runner_active": self.task is not None and not self.task.done(),
            "runner_error": self.runner_error,
            "worker_recovery": {"restart_count": self.restart_count, "retry_at": self.retry_at},
            "external_changes": self.disk_revision() != self.revision,
            "events": self.watcher.events(limit=20).data
            if self.watcher
            else {
                "events": [],
                "total": 0,
                "next_offset": None,
            },
            "query_logs": query_logs,
        }


CONTROLLER = web.AppKey("controller", Controller)


@web.middleware
async def protect(request, handler):
    controller = request.app[CONTROLLER]
    try:
        host = urlsplit("http://" + request.host).hostname
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden()
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), controller.csrf):
                raise web.HTTPForbidden()
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType()
        response = await handler(request)
    except Conflict as error:
        response = web.json_response({"error": str(error)}, status=409)
    except (ValueError, KeyError, TypeError, yaml.YAMLError):
        # Do not echo malformed input, credential URLs or filesystem paths.
        response = web.json_response(
            {"error": "設定格式不正確，請確認網址、頻道及數值範圍。"}, status=400
        )
    except OSError:
        response = web.json_response({"error": "無法儲存設定，請確認資料目錄可寫入。"}, status=500)
    except web.HTTPException as error:
        response = web.json_response(
            {"error": "請求未通過驗證或找不到此操作，請重新整理。"}, status=error.status
        )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        }
    )
    return response


async def state(request):
    async with request.app[CONTROLLER].lock:
        return web.json_response(request.app[CONTROLLER].state())


async def mutate(request):
    c = request.app[CONTROLLER]
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError
    async with c.lock:
        c.require_revision(body.get("revision"))
        document = copy.deepcopy(c.document)
        credentials = read_webhooks(c.config.secrets_path)
        kind, ident = request.match_info["kind"], request.match_info.get("ident")
        if kind == "channels" and ident == "default":
            if request.method == "DELETE":
                credentials.pop("default", None)
            else:
                value = body.get("value")
                if not isinstance(value, dict) or set(value) - {"name", "webhook_url"}:
                    raise ValueError
                if value.get("webhook_url"):
                    credentials["default"] = validate_webhook(value["webhook_url"])
        elif kind in {"targets", "channels"}:
            entries = document.setdefault(kind, [])
            existing = next((item for item in entries if item["id"] == ident), None)
            if ident and not existing:
                raise web.HTTPNotFound()
            if request.method == "DELETE":
                if kind == "channels" and any(t.channel_id == ident for t in c.config.targets):
                    raise Conflict("仍有監控使用此頻道，請先更換監控的通知頻道。")
                if kind == "channels" and c.config.worker_alert_channel == ident:
                    raise Conflict("服務異常通知仍使用此頻道，請先更換通知目的地。")
                entries.remove(existing)
                if kind == "channels":
                    credentials.pop(ident, None)
            else:
                value = body.get("value")
                if not isinstance(value, dict):
                    raise ValueError
                if kind == "channels":
                    if set(value) - {"name", "webhook_url"}:
                        raise ValueError
                    ident = ident or "dc-" + uuid.uuid4().hex[:12]
                    url = value.get("webhook_url", "")
                    if url:
                        credentials[ident] = validate_webhook(url)
                    elif not existing:
                        raise ValueError
                    updated = {"id": ident, "name": value["name"]}
                else:
                    if len(entries) >= 100 and not existing:
                        raise Conflict("最多新增 100 個監控目標。")
                    if set(value) - {
                        "name",
                        "url",
                        "enabled",
                        "session_ids",
                        "item_ids",
                        "stop_at",
                        "channel_id",
                        "auto_stop",
                    }:
                        raise ValueError
                    if (
                        not isinstance(value.get("name"), str)
                        or not 1 <= len(value["name"].strip()) <= 120
                    ):
                        raise ValueError
                    updated = {
                        **(existing or {}),
                        **value,
                        "id": ident or "watch-" + uuid.uuid4().hex[:12],
                    }
                    updated.setdefault("enabled", False)
                if existing:
                    entries[entries.index(existing)] = updated
                else:
                    entries.append(updated)
        elif kind == "settings":
            value = body.get("value")
            if not isinstance(value, dict) or set(value) - set(SETTINGS):
                raise ValueError
            for section, fields in value.items():
                if not isinstance(fields, dict) or set(fields) - SETTINGS[section]:
                    raise ValueError
                document.setdefault(section, {}).update(fields)
        else:
            raise web.HTTPNotFound()
        await c.save(document, credentials)
        return web.json_response(c.state())


async def action(request):
    c = request.app[CONTROLLER]
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError
    async with c.lock:
        c.require_revision(body.get("revision"))
        if c.watcher is None:
            raise Conflict("監控正在重新啟動，請稍後再試。")
        kind = request.match_info["action"]
        if kind == "test-channel":
            ident = body.get("channel_id")
            if ident not in channel_urls(c.config):
                raise Conflict("此頻道尚未設定 Discord Webhook。")
            event_id = str(uuid.uuid4())
            with c.watcher.store.transaction() as db:
                c.watcher.store.enqueue(
                    db,
                    event_id,
                    None,
                    "SYSTEM",
                    c.watcher.clock(),
                    {"message": "測試通知：此頻道已連接 Ticket Watcher。"},
                    c.config.notification_ttl,
                    True,
                    channel_id=ident,
                )
            task = c.begin_action(deliver_test(c.watcher, event_id))
        elif kind == "check":
            task = c.begin_action(manual_check(c.watcher, body.get("target_id")))
        elif kind == "resume":
            return web.json_response(
                c.watcher.resume(
                    body.get("target_id"), platform=body.get("platform") is True
                ).to_dict()
            )
        else:
            raise web.HTTPNotFound()
    # The watcher is borrowed by a tracked task. Reload/recovery cancels and
    # drains it before closing its connections, without blocking status reads.
    try:
        return web.json_response(await task)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():
            raise
        raise Conflict("設定已重新載入或監控正在重啟，本次操作已中止。") from None


async def manual_check(watcher, ident):
    result = await watcher._check(ident, immediate=True, mode="manual")
    # The running delivery loop handles the outbox; don't wait for Discord here.
    return watcher._notification_result(result).to_dict()


async def deliver_test(watcher, event_id):
    await watcher.notifier.deliver(max_messages=1, event_id=event_id)
    return {"notification": watcher.store.notification_status(event_id)}


def create_app(path: Path, *, watcher_factory=Watcher, monitor=True):
    app = web.Application(middlewares=[protect], client_max_size=65536)
    app[CONTROLLER] = Controller(path, watcher_factory=watcher_factory, monitor=monitor)

    async def lifecycle(app):
        controller = app[CONTROLLER]
        await controller.start()
        try:
            yield
        finally:
            await controller.close()

    app.cleanup_ctx.append(lifecycle)

    async def asset(request):
        name = request.match_info.get("file", "index.html")
        if name not in {
            "index.html",
            "app.css",
            "app.js",
            "ticket-watcher-mark-inverse.svg",
            "favicon.svg",
            "favicon.ico",
            "apple-touch-icon.png",
        }:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app.router.add_get("/", asset)
    app.router.add_get("/{file:favicon\\.ico}", asset)
    app.router.add_get("/static/{file}", asset)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/actions/{action}", action)
    app.router.add_post("/api/{kind:targets|channels|settings}", mutate)
    app.router.add_put("/api/{kind:targets|channels}/{ident}", mutate)
    app.router.add_delete("/api/{kind:targets|channels}/{ident}", mutate)
    return app


async def serve(path: Path, host="127.0.0.1", port=8787):
    if host not in {"127.0.0.1", "localhost", "::1", "0.0.0.0"} or not 1 <= port <= 65535:
        raise ValueError("UI 位址或連接埠不正確")
    runner = web.AppRunner(create_app(path), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
        log.info("ui_ready url=http://localhost:%d", port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
