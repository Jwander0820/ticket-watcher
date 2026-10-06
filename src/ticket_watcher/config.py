import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml


def validate_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "ticketplus.com.tw"
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or not re.fullmatch(
            r"/(?:activity/(?:[a-f0-9]{32}|e[0-9]{9})"
            r"|order/(?:[a-f0-9]{32}|e[0-9]{9})/(?:[a-f0-9]{32}|s[0-9]{9}))/?",
            parts.path,
        )
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "需使用公開 TicketPlus 活動或場次購票網址（activity/<ID> 或 order/<活動ID>/<場次ID>）"
        )
    return "https://ticketplus.com.tw" + parts.path.rstrip("/")


@dataclass(frozen=True)
class Target:
    id: str
    name: str
    url: str
    enabled: bool = True
    source: str = "auto"
    session_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    stop_at: float | None = None
    platform: str = "ticketplus"
    channel_id: str = "default"

    @property
    def signature(self) -> str:
        raw = json.dumps([self.url, self.source, sorted(self.session_ids), sorted(self.item_ids)])
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Channel:
    id: str
    name: str


@dataclass(frozen=True)
class Config:
    database_path: Path = Path("data/watcher.db")
    timezone: str = "Asia/Taipei"
    normal_interval: tuple[int, int] = (300, 900)
    active_interval: tuple[int, int] = (60, 180)
    active_window: int = 1800
    exit_active_checks: int = 2
    request_gap: int = 5
    timeout: int = 20
    backoff: tuple[int, ...] = (900, 1800, 3600)
    webhook_url_env: str = "DISCORD_WEBHOOK_URL"
    system_alerts: bool = True
    worker_alerts: bool = True
    worker_alert_channel: str = "default"
    notification_ttl: int = 600
    retry_delays: tuple[int, ...] = (10, 30, 60, 120, 300)
    retention_days: int = 30
    targets: tuple[Target, ...] = field(default_factory=tuple)
    channels: tuple[Channel, ...] = field(default_factory=tuple)

    @property
    def secrets_path(self) -> Path:
        return self.database_path.with_name("discord-webhooks.json")


def _section(data: dict, key: str, allowed: set[str]) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{key} 設定欄位不正確")
    return value


def _int(value, minimum=1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"數值必須是大於等於 {minimum} 的整數")
    return value


def _bool(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("布林欄位必須使用 true 或 false")
    return value


def _interval(value, minimum: int) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("間隔必須使用 [最小秒數, 最大秒數]")
    low, high = (_int(x, minimum) for x in value)
    if high < low:
        raise ValueError("最大間隔不可小於最小間隔")
    return low, high


def _sequence(value) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("重試設定必須是非空秒數陣列")
    return tuple(_int(x) for x in value)


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        return Config(database_path=Path("data/watcher.db").resolve())
    path = Path(path).resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    return parse_config(data, path.parent)


def parse_config(data: dict, directory: Path) -> Config:
    if not isinstance(data, dict) or set(data) - {
        "app",
        "polling",
        "http",
        "notifications",
        "targets",
        "channels",
    }:
        raise ValueError("設定檔結構不正確")
    app = _section(data, "app", {"timezone", "database_path", "log_level", "retention_days"})
    polling = _section(
        data,
        "polling",
        {
            "normal_interval_seconds",
            "active_interval_seconds",
            "active_window_seconds",
            "exit_active_after_no_available_checks",
        },
    )
    http = _section(
        data,
        "http",
        {"platform_concurrency", "min_request_gap_seconds", "backoff_seconds", "timeout_seconds"},
    )
    notice = _section(
        data,
        "notifications",
        {
            "webhook_url_env",
            "system_alerts_enabled",
            "worker_alerts_enabled",
            "worker_alert_channel_id",
            "delivery_ttl_seconds",
            "retry_delays_seconds",
        },
    )
    if http.get("platform_concurrency", 1) != 1:
        raise ValueError("第一版平台並行請求數必須是 1")
    zone = app.get("timezone", "Asia/Taipei")
    ZoneInfo(zone)
    env_name = notice.get("webhook_url_env", "DISCORD_WEBHOOK_URL")
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("Webhook 環境變數名稱不正確")
    database = Path(app.get("database_path", "data/watcher.db"))
    channels = []
    entries = data.get("channels", [])
    if not isinstance(entries, list) or len(entries) > 30:
        raise ValueError("通知頻道必須是陣列，最多 30 個")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "name"}:
            raise ValueError("通知頻道需提供 id 與 name")
        ident, name = entry["id"], entry["name"]
        if (
            not isinstance(ident, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ident)
            or ident == "default"
            or ident in {c.id for c in channels}
        ):
            raise ValueError("通知頻道 ID 不正確或重複")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("通知頻道名稱需為 1 至 80 個字")
        channels.append(Channel(ident, name.strip()))
    worker_channel = notice.get("worker_alert_channel_id", "default")
    if not isinstance(worker_channel, str) or worker_channel not in {
        "default",
        *(c.id for c in channels),
    }:
        raise ValueError("服務異常通知指定的 Discord 頻道不存在")
    targets = []
    entries = data.get("targets", [])
    if not isinstance(entries, list):
        raise ValueError("targets 必須是陣列")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {
            "id",
            "name",
            "platform",
            "url",
            "enabled",
            "source",
            "session_ids",
            "item_ids",
            "stop_at",
            "channel_id",
        }:
            raise ValueError("監控目標欄位不正確")
        ident = entry.get("id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ident):
            raise ValueError("目標 id 必須是 1 到 80 個英數字、底線或連字號")
        if ident in {target.id for target in targets}:
            raise ValueError("目標 id 不可重複")
        if entry.get("platform", "ticketplus") != "ticketplus":
            raise ValueError("第一版只支援 ticketplus")
        if entry.get("source", "auto") not in {"auto", "api"}:
            raise ValueError("目前可驗證來源僅支援 source: auto 或 api")
        filters = []
        for key in ("session_ids", "item_ids"):
            values = entry.get(key, [])
            if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
                raise ValueError(f"{key} 必須是非空 ID 字串的陣列")
            filters.append(tuple(values))
        stop = entry.get("stop_at")
        if stop:
            stop = datetime.fromisoformat(str(stop))
            if stop.tzinfo is None:
                raise ValueError("stop_at 必須包含時區")
            stop = stop.timestamp()
        channel = entry.get("channel_id", "default")
        if channel not in {"default", *(c.id for c in channels)}:
            raise ValueError("監控指定的 Discord 頻道不存在")
        url = validate_url(entry["url"])
        if filters[1] and "/activity/" in url:
            raise ValueError("票區或票種篩選需使用 order 場次網址")
        targets.append(
            Target(
                ident,
                str(entry.get("name", ident)),
                url,
                _bool(entry.get("enabled", True)),
                entry.get("source", "auto"),
                *filters,
                stop,
                channel_id=channel,
            )
        )
    return Config(
        database_path=(directory / database).resolve(),
        timezone=zone,
        normal_interval=_interval(polling.get("normal_interval_seconds", [300, 900]), 300),
        active_interval=_interval(polling.get("active_interval_seconds", [60, 180]), 60),
        active_window=_int(polling.get("active_window_seconds", 1800)),
        exit_active_checks=_int(polling.get("exit_active_after_no_available_checks", 2)),
        request_gap=_int(http.get("min_request_gap_seconds", 5), 5),
        timeout=_int(http.get("timeout_seconds", 20)),
        backoff=_sequence(http.get("backoff_seconds", [900, 1800, 3600])),
        webhook_url_env=env_name,
        system_alerts=_bool(notice.get("system_alerts_enabled", True)),
        worker_alerts=_bool(notice.get("worker_alerts_enabled", True)),
        worker_alert_channel=worker_channel,
        notification_ttl=_int(notice.get("delivery_ttl_seconds", 600)),
        retry_delays=_sequence(notice.get("retry_delays_seconds", [10, 30, 60, 120, 300])),
        retention_days=_int(app.get("retention_days", 30)),
        targets=tuple(targets),
        channels=tuple(channels),
    )
