import asyncio
import json
import math
import os
import re
from urllib.parse import urlsplit

import httpx

from .config import Config
from .http_body import ACCEPT_ENCODING, DISCORD_BODY_LIMIT, ResponseBodyError, read_body
from .models import utcnow
from .notification_text import discord_length, plain, ticket_content
from .private_io import read_webhooks
from .schedule import stop_info, stopped_sessions
from .storage import Store
from .transport import retry_after

SUPPRESS_EMBEDS = 1 << 2


def webhook_url(env_name: str) -> str | None:
    value = os.environ.get(env_name)
    if not value:
        return None
    return validate_webhook(value)


def validate_webhook(value: str) -> str:
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "https"
            and parts.hostname == "discord.com"
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.query
            and not parts.fragment
            and re.fullmatch(r"/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9_.-]+", parts.path)
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Discord webhook 環境變數格式不正確（值已隱藏）")
    return value


def channel_urls(config: Config) -> dict[str, str]:
    urls = {}
    stored = read_webhooks(config.secrets_path)
    default = (
        validate_webhook(stored["default"])
        if stored.get("default")
        else webhook_url(config.webhook_url_env)
    )
    if default:
        urls["default"] = default
    for channel in config.channels:
        if stored.get(channel.id):
            urls[channel.id] = validate_webhook(stored[channel.id])
    return urls


class DiscordNotifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        store: Store,
        config: Config,
        clock=utcnow,
        *,
        on_change=None,
    ):
        self.client, self.store, self.config, self.clock = client, store, config, clock
        self.on_change = on_change

    def next_delivery_at(self) -> float | None:
        return self.store.next_notice_at(self.clock(), set(channel_urls(self.config)))

    def _format(self, notice: dict) -> dict | None:
        payload = notice["payload"]
        stopped = set()
        if payload.get("worker_alert") and (
            not self.config.worker_alerts
            or notice["channel_id"] not in self.config.worker_notification_channels
        ):
            return None
        if notice["target_id"]:
            target = next((t for t in self.config.targets if t.id == notice["target_id"]), None)
            state = self.store.target(notice["target_id"])
            if (
                not target
                or not target.enabled
                or not state
                or target.signature != state["signature"]
                or payload.get("target_signature", state["signature"]) != state["signature"]
                or notice["channel_id"] not in target.notification_channels
                or stop_info(target, self.store.target_schedule(target), self.clock())[
                    "stop_reason"
                ]
            ):
                return None
            stopped = stopped_sessions(target, self.store.target_schedule(target), self.clock())
        if notice["kind"] == "SYSTEM":
            title = payload.get("title", "Ticket Watcher")
            if payload.get("event_name"):
                title += "｜" + plain(payload["event_name"])
            content = f"{title}\n{payload['message']}"
        else:
            states = {x["item_key"]: x for x in self.store.items(notice["target_id"])}
            excluded = set(json.loads(notice["excluded_items"]))
            expected = (
                "TEMPORARILY_UNAVAILABLE" if notice["kind"] == "RELEASE_HINT" else "AVAILABLE"
            )
            changes = [
                x
                for x in payload["changes"]
                if x["item_key"] not in excluded
                and x["item"]["session_id"] not in stopped
                and x["item_key"] in states
                and states[x["item_key"]]["last_valid"] == x.get("current", expected)
            ]
            if not changes:
                return None
            sessions = dict.fromkeys(x["item"]["session_id"] for x in changes)
            snapshot = payload.get("snapshot", [x["item"] for x in changes])
            contents = []
            for session_id in sessions:
                items = [
                    item
                    for item in snapshot
                    if item["session_id"] == session_id
                    and item["item_key"] not in excluded
                    and item["item_key"] in states
                    and states[item["item_key"]]["last_valid"] == item["status"]
                ]
                items.sort(key=lambda item: item["status"] != "AVAILABLE")
                displayed = [
                    item
                    for item in items
                    if "display_keys" not in payload or item["item_key"] in payload["display_keys"]
                ]
                if not displayed:
                    continue
                contents.append(
                    ticket_content(
                        {
                            **payload,
                            "observed_at": payload.get(
                                "observed_at", max(x["observed_at"] for x in changes)
                            ),
                        },
                        displayed,
                        self.config.timezone,
                        session_items=items,
                    )
                )
            if not contents:
                return None
            content = "\n\n".join(contents)
            # New notices are durably partitioned before enqueueing. A legacy
            # queued event can contain many sessions in a single delivery.
            if discord_length(content) > 2000:
                content = contents[0]
                for section in contents[1:]:
                    if discord_length(content + "\n\n" + section) > 1900:
                        content += "\n更多項目請至控制台事件紀錄查看。"
                        break
                    content += "\n\n" + section
        return {
            "content": content[:2000],
            "allowed_mentions": {"parse": []},
            "flags": SUPPRESS_EMBEDS,
        }

    async def deliver(self, max_messages: int = 10, *, event_id: str | None = None) -> dict:
        try:
            return await self._deliver(max_messages, event_id=event_id)
        finally:
            if self.on_change:
                self.on_change()

    async def _deliver(self, max_messages: int, *, event_id: str | None) -> dict:
        urls = channel_urls(self.config)
        if not urls:
            # Expire old work even when delivery has not been configured.
            self.store.connection.execute(
                """UPDATE outbox SET status='EXPIRED' WHERE
             status IN ('PENDING','INFLIGHT') AND expires_at<=?""",
                (self.clock(),),
            )
            return {"sent": 0, "reason": "WEBHOOK_NOT_CONFIGURED"}
        sent = 0
        for _ in range(max_messages):
            notice = self.store.claim_notice(
                self.clock(),
                len(self.config.retry_delays) + 1,
                lease_seconds=max(120, self.config.timeout + 30),
                channels=set(urls),
                event_id=event_id,
            )
            if not notice:
                break
            url = urls[notice["channel_id"]]
            payload = self._format(notice)
            if payload is None:
                self.store.finish_notice(notice, self.clock(), "CANCELLED")
                continue
            delay, error, message_id, terminal = 0, "DELIVERY_FAILED", None, False
            try:
                # Bound the whole request, including a response that trickles in,
                # so an active sender cannot outlive its database lease.
                async with asyncio.timeout(self.config.timeout):
                    async with self.client.stream(
                        "POST",
                        url,
                        params={"wait": "true"},
                        json=payload,
                        headers={"Accept-Encoding": ACCEPT_ENCODING},
                    ) as response:
                        if response.status_code == 429:
                            error = "RATE_LIMITED"
                            delay = retry_after(response.headers.get("Retry-After"), self.clock())
                        elif 400 <= response.status_code < 500:
                            error, terminal = f"HTTP_{response.status_code}", True
                        body = (
                            await read_body(response, DISCORD_BODY_LIMIT)
                            if response.status_code == 429 or 200 <= response.status_code < 300
                            else b""
                        )
                if response.status_code == 429:
                    try:
                        server_delay = float(json.loads(body).get("retry_after", 0))
                        if math.isfinite(server_delay):
                            delay = max(delay, server_delay)
                    except (ValueError, TypeError, AttributeError):
                        pass
                elif 200 <= response.status_code < 300:
                    try:
                        value = json.loads(body).get("id")
                        if isinstance(value, str) and value:
                            message_id = value
                        else:
                            error = "ACKNOWLEDGEMENT_MISSING"
                    except (ValueError, AttributeError):
                        error = "ACKNOWLEDGEMENT_MISSING"
            except ResponseBodyError:
                if error != "RATE_LIMITED":
                    error = "INVALID_RESPONSE_BODY"
            except (httpx.HTTPError, TimeoutError):
                if error != "RATE_LIMITED":
                    error = "NETWORK_OR_TIMEOUT"
            now = self.clock()
            if message_id:
                if self.store.finish_notice(notice, now, "SENT", message_id=message_id):
                    sent += 1
            else:
                attempt = notice["attempts"]
                exhausted = attempt > len(self.config.retry_delays)
                local_delay = self.config.retry_delays[
                    min(attempt - 1, len(self.config.retry_delays) - 1)
                ]
                next_attempt = now + max(delay, local_delay)
                status = "FAILED" if terminal or exhausted else "PENDING"
                if now >= notice["expires_at"] or (
                    not terminal and not exhausted and next_attempt >= notice["expires_at"]
                ):
                    status = "EXPIRED"
                self.store.finish_notice(
                    notice,
                    now,
                    status,
                    error=error,
                    next_attempt=next_attempt,
                    blocked_until=next_attempt if error == "RATE_LIMITED" else 0,
                )
            if error == "RATE_LIMITED" and not message_id:
                break
        return {"sent": sent}
