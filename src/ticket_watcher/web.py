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
from .notifications import channel_urls, validate_webhook
from .private_io import atomic_text, read_webhooks
from .service import Watcher

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
SETTINGS = {
    "polling": {
        "normal_interval_seconds",
        "active_interval_seconds",
        "active_window_seconds",
        "exit_active_after_no_available_checks",
    },
    "http": {"min_request_gap_seconds", "timeout_seconds"},
    "notifications": {"system_alerts_enabled", "delivery_ttl_seconds"},
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
        self.watcher = self.factory(self.config)
        self.runner_error = None
        if self.monitor:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            await self.watcher.run()
        except Exception as error:
            self.runner_error = "監控程序已停止，請檢查設定後重新啟動服務。"
            log.error("ui_worker_failed type=%s", type(error).__name__)

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.watcher:
            await self.watcher.__aexit__(None, None, None)
            self.watcher = None

    def require_revision(self, value):
        if value != self.revision or self.disk_revision() != self.revision:
            raise Conflict(
                "設定已在其他視窗或檔案中變更。請重新整理；若曾手動編輯檔案，請重啟 UI。"
            )

    async def save(self, document: dict, credentials: dict):
        config = parse_config(document, self.path.parent)
        for value in credentials.values():
            validate_webhook(value)
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
                "delivery_ttl_seconds": c.notification_ttl,
            },
        }

    def state(self):
        states = {s["target_id"]: s for s in self.watcher.status().data["targets"]}
        targets = []
        for target in self.config.targets:
            value = asdict(target)
            value["stop_at"] = (
                datetime.fromtimestamp(target.stop_at, UTC).isoformat() if target.stop_at else None
            )
            targets.append({**value, "state": states.get(target.id, {})})
        configured = set(channel_urls(self.config))
        return {
            "csrf": self.csrf,
            "revision": self.revision,
            "settings": self.settings(),
            "targets": targets,
            "channels": [
                {
                    "id": "default",
                    "name": "預設頻道（環境變數）",
                    "configured": "default" in configured,
                    "readonly": True,
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
            "health": self.watcher.health().data,
            "runner_active": self.task is not None and not self.task.done(),
            "runner_error": self.runner_error,
            "external_changes": self.disk_revision() != self.revision,
            "events": self.watcher.events(limit=20).data,
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
        if kind in {"targets", "channels"}:
            entries = document.setdefault(kind, [])
            existing = next((item for item in entries if item["id"] == ident), None)
            if ident and not existing:
                raise web.HTTPNotFound()
            if request.method == "DELETE":
                if kind == "channels" and any(t.channel_id == ident for t in c.config.targets):
                    raise Conflict("仍有監控使用此頻道，請先更換監控的通知頻道。")
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
            await c.watcher.notifier.deliver(max_messages=1, event_id=event_id)
            return web.json_response(
                {"notification": c.watcher.store.notification_status(event_id)}
            )
        if kind == "check":
            return web.json_response((await c.watcher.check(body.get("target_id"))).to_dict())
        if kind == "resume":
            return web.json_response(
                c.watcher.resume(
                    body.get("target_id"), platform=body.get("platform") is True
                ).to_dict()
            )
        raise web.HTTPNotFound()


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
        if name not in {"index.html", "app.css", "app.js"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app.router.add_get("/", asset)
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
