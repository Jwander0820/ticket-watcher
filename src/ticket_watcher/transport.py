import asyncio
import json
import math
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .config import Config
from .http_body import ACCEPT_ENCODING, PUBLIC_BODY_LIMIT, ResponseBodyError, read_body
from .models import SourceError, utcnow
from .storage import Store


def retry_after(value: str | None, now: float) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0, (date - datetime.fromtimestamp(now, UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class PublicTransport:
    def __init__(self, client: httpx.AsyncClient, store: Store, config: Config, clock=utcnow):
        self.client, self.store, self.config, self.clock = client, store, config, clock
        # A late request and its replacement can share this transport in the UI.
        # Keep their identities/counters local to each task, not to the client.
        self._owner = ContextVar("query_owner", default="")
        self._count = ContextVar("query_count", default=0)
        self.lease_seconds = max(180, config.timeout * 4 + config.request_gap * 4 + 60)

    @property
    def owner(self):
        return self._owner.get()

    @owner.setter
    def owner(self, value):
        self._owner.set(value)

    @property
    def count(self):
        return self._count.get()

    @count.setter
    def count(self, value):
        self._count.set(value)

    async def get_json(self, url: str, params: dict) -> dict:
        while True:
            delay = self.store.reserve_request(
                self.owner, self.clock(), self.config.request_gap, self.lease_seconds
            )
            if not delay:
                break
            await asyncio.sleep(min(delay, 30))
        self.count += 1
        try:
            # HTTPX read timeouts bound each chunk, not the entire response.
            async with asyncio.timeout(self.config.timeout):
                async with self.client.stream(
                    "GET", url, params=params, headers={"Accept-Encoding": ACCEPT_ENCODING}
                ) as response:
                    if response.status_code in (401, 403):
                        raise SourceError(
                            "BLOCKED", f"網站拒絕存取（HTTP {response.status_code}），需人工處理"
                        )
                    if response.status_code == 429:
                        raise SourceError(
                            "RATE_LIMITED",
                            "網站限制請求頻率",
                            retry_after(response.headers.get("Retry-After"), self.clock()),
                        )
                    if response.status_code >= 500:
                        raise SourceError("NETWORK", "售票網站暫時異常")
                    if response.status_code != 200:
                        raise SourceError(
                            "PARSE", f"資料來源回傳非預期 HTTP {response.status_code}"
                        )
                    body = await read_body(response, PUBLIC_BODY_LIMIT)
        except ResponseBodyError as error:
            raise SourceError("PARSE", str(error)) from None
        except (httpx.HTTPError, TimeoutError):
            raise SourceError("NETWORK", "網路連線失敗或逾時") from None
        if "text/html" in response.headers.get("content-type", "").lower():
            page = body.decode("utf-8", errors="replace").lower()
            if any(
                marker in page
                for marker in ("captcha", "cf-chl-", "challenge-platform", "verify you are human")
            ):
                raise SourceError("BLOCKED", "網站要求驗證，需人工處理")
            raise SourceError("PARSE", "API 回傳 HTML，未取得可靠票況")
        try:
            value = json.loads(body)
        except ValueError:
            raise SourceError("PARSE", "資料來源不是有效 JSON") from None
        if not isinstance(value, dict):
            raise SourceError("PARSE", "JSON 資料結構不正確")
        self.store.require_lease(self.owner, self.clock())
        return value
