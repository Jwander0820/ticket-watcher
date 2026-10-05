import json
import math
import os
import re
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .models import utcnow
from .storage import Store
from .transport import retry_after


def webhook_url(env_name: str) -> str | None:
    value = os.environ.get(env_name)
    if not value:
        return None
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
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Discord webhook 環境變數格式不正確（值已隱藏）")
    return value


class DiscordNotifier:
    def __init__(self, client: httpx.AsyncClient, store: Store, config: Config, clock=utcnow):
        self.client, self.store, self.config, self.clock = client, store, config, clock

    def _format(self, notice: dict) -> dict | None:
        payload = notice["payload"]
        if notice["target_id"]:
            target = next((t for t in self.config.targets if t.id == notice["target_id"]), None)
            state = self.store.target(notice["target_id"])
            if (
                not target
                or not target.enabled
                or not state
                or target.signature != state["signature"]
                or (target.stop_at is not None and target.stop_at <= self.clock())
            ):
                return None
        if notice["kind"] == "SYSTEM":
            content = f"Ticket Watcher｜{payload['message']}\n事件：{notice['event_id']}"
        else:
            states = {x["item_key"]: x for x in self.store.items(notice["target_id"])}
            excluded = set(json.loads(notice["excluded_items"]))
            changes = [
                x
                for x in payload["changes"]
                if x["item_key"] not in excluded
                and x["item_key"] in states
                and states[x["item_key"]]["last_valid"] == "AVAILABLE"
            ]
            if not changes:
                return None
            zone = ZoneInfo(self.config.timezone)

            def display(value):
                return datetime.fromtimestamp(value, zone).strftime("%Y/%m/%d %H:%M:%S %Z")

            lines = [
                f"Ticket Watcher｜偵測到可購票\n活動：{payload['event_name'][:180]}",
                f"監控粒度：場次｜本輪釋票項目：{len(changes)}",
            ]
            # All items were evaluated and saved; only the message presentation is bounded.
            for change in changes:
                item = change["item"]
                line = (
                    f"\n場次：{item['name'][:120]} ({item['session_id']})\n"
                    f"日期／時間：{item.get('date') or '來源未提供'} {item.get('time') or ''}\n"
                    f"場館：{item.get('venue') or '來源未提供'}\n"
                    f"狀態：售完 → 可購買\n最後確認售完：{display(change['previous_observed_at'])}\n"
                    f"首次偵測有票：{display(change['observed_at'])}"
                )
                if change["monitoring_gap"]:
                    line += "\n中間存在超過 45 分鐘的監控空窗"
                if sum(len(x) for x in lines) + len(line) > 1400:
                    lines.append("\n更多項目請使用 events 查閱完整紀錄。")
                    break
                lines.append(line)
            lines.extend(
                [
                    f"\n購票連結：{payload['public_url']}",
                    f"事件：{notice['event_id']}",
                    "這是查詢當下的觀測結果，不代表已保留票券或精確放票時間。",
                ]
            )
            content = "\n".join(lines)
        return {"content": content[:2000], "allowed_mentions": {"parse": []}}

    async def deliver(self, max_messages: int = 10) -> dict:
        url = webhook_url(self.config.webhook_url_env)
        if not url:
            # Expire old work even when delivery has not been configured.
            self.store.connection.execute(
                """UPDATE outbox SET status='EXPIRED' WHERE
             status IN ('PENDING','INFLIGHT') AND expires_at<=?""",
                (self.clock(),),
            )
            return {"sent": 0, "reason": "WEBHOOK_NOT_CONFIGURED"}
        sent = 0
        for _ in range(max_messages):
            notice = self.store.claim_notice(self.clock(), len(self.config.retry_delays) + 1)
            if not notice:
                break
            payload = self._format(notice)
            if payload is None:
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE outbox SET status='CANCELLED',lease_until=NULL WHERE event_id=?",
                        (notice["event_id"],),
                    )
                    db.execute(
                        "UPDATE platform SET lease_owner=NULL,lease_until=0 WHERE id='discord' AND lease_owner=?",
                        (notice["event_id"],),
                    )
                continue
            delay, error, message_id, terminal = 0, "DELIVERY_FAILED", None, False
            try:
                response = await self.client.post(url, params={"wait": "true"}, json=payload)
                if response.status_code == 429:
                    error = "RATE_LIMITED"
                    delay = retry_after(response.headers.get("Retry-After"), self.clock())
                    try:
                        server_delay = float(response.json().get("retry_after", 0))
                        if math.isfinite(server_delay):
                            delay = max(delay, server_delay)
                    except (ValueError, TypeError, AttributeError):
                        pass
                elif 200 <= response.status_code < 300:
                    try:
                        value = response.json().get("id")
                        if isinstance(value, str) and value:
                            message_id = value
                        else:
                            error = "ACKNOWLEDGEMENT_MISSING"
                    except (ValueError, AttributeError):
                        error = "ACKNOWLEDGEMENT_MISSING"
                elif 400 <= response.status_code < 500:
                    error, terminal = f"HTTP_{response.status_code}", True
            except httpx.HTTPError:
                error = "NETWORK_OR_TIMEOUT"
            now = self.clock()
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE platform SET lease_owner=NULL,lease_until=0 WHERE id='discord' AND lease_owner=?",
                    (notice["event_id"],),
                )
                if message_id:
                    db.execute(
                        "UPDATE outbox SET status='SENT',message_id=?,lease_until=NULL,last_error=NULL WHERE event_id=?",
                        (message_id, notice["event_id"]),
                    )
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
                    db.execute(
                        "UPDATE outbox SET status=?,next_attempt=?,lease_until=NULL,last_error=? WHERE event_id=?",
                        (status, next_attempt, error, notice["event_id"]),
                    )
                    if error == "RATE_LIMITED":
                        db.execute(
                            "UPDATE platform SET blocked_until=max(blocked_until,?) WHERE id='discord'",
                            (next_attempt,),
                        )
            if error == "RATE_LIMITED" and not message_id:
                break
        return {"sent": sent}
