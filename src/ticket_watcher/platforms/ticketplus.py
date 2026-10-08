import re

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..config import Target, validate_url
from ..models import Observation, SourceError, TicketItem, TicketStatus
from ..schedule import session_start
from ..transport import PublicTransport

# Public URL obfuscation constants from TicketPlus's own frontend module 9263.
# These are not account credentials and do not provide access to protected endpoints.
_KEY = b"ILOVEFETIXFETIX!"
_IV = b"!@#$FETIXEVENTiv"
STATIC_API = "https://apis.ticketplus.com.tw/config/api/v1/getS3"
STATUS_API = "https://apis.ticketplus.com.tw/config/api/v1/get"
STATUS_MAP = {
    "onsale": TicketStatus.AVAILABLE,
    "soldout": TicketStatus.SOLD_OUT,
    "pending": TicketStatus.UPCOMING,
    "over": TicketStatus.ENDED,
    "unavailable": TicketStatus.TEMPORARILY_UNAVAILABLE,
    "lock": TicketStatus.PAUSED,
}


def internal_id(value: str, kind: str) -> str:
    if re.fullmatch(kind + r"[0-9]{9}", value):
        return value
    try:
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError
        decryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).decryptor()
        raw = decryptor.update(bytes.fromhex(value)) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        result = (unpadder.update(raw) + unpadder.finalize()).decode("ascii")
        if not re.fullmatch(kind + r"[0-9]{9}", result):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise SourceError("UNSUPPORTED", "活動或場次 ID 不符合已驗證的 TicketPlus 格式") from None


def public_id(value: str) -> str:
    padder = padding.PKCS7(128).padder()
    raw = padder.update(value.encode("ascii")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).encryptor()
    return (encryptor.update(raw) + encryptor.finalize()).hex()


class TicketPlusAdapter:
    granularity = "SESSION"

    def __init__(self, transport: PublicTransport):
        self.transport = transport

    async def fetch(self, target: Target) -> Observation:
        url = validate_url(target.url)
        path = url.split("/")[3:]
        order_session = internal_id(path[2], "s") if path[0] == "order" else None
        wanted = {internal_id(x, "s") for x in target.session_ids}
        if order_session and wanted and wanted != {order_session}:
            raise SourceError("UNSUPPORTED", "購票網址的場次與 session_ids 不一致")
        if target.item_ids and not order_session:
            raise SourceError(
                "UNSUPPORTED", "活動外頁只提供場次票況，票區或票種 item_ids 需使用購票網址"
            )
        event_id = internal_id(path[1], "e")
        event_public_id = public_id(event_id)
        url = f"https://ticketplus.com.tw/activity/{event_public_id}"
        before = self.transport.count
        metadata = self.transport.store.metadata(event_id, self.transport.clock())
        cached = metadata is not None
        if metadata is None:
            event = await self.transport.get_json(
                STATIC_API, {"path": f"event/{event_public_id}/event.json"}
            )
            sessions = await self.transport.get_json(
                STATIC_API, {"path": f"event/{event_public_id}/sessions.json"}
            )
            if not isinstance(event.get("title"), str) or not isinstance(
                sessions.get("sessions"), list
            ):
                raise SourceError("PARSE", "活動或場次靜態資料結構改變")
            metadata = {"title": event["title"], "sessions": sessions["sessions"]}
            self.transport.store.cache_metadata(
                event_id,
                metadata,
                self.transport.clock() + 3600,
                owner=self.transport.owner,
                clock=self.transport.clock,
            )
        visible = []
        for session in metadata["sessions"]:
            if not isinstance(session, dict):
                raise SourceError("PARSE", "場次資料結構改變")
            if session.get("hidden") is True:
                continue
            if not isinstance(session.get("sessionId"), str):
                raise SourceError("PARSE", "場次缺少穩定 ID")
            visible.append((internal_id(session["sessionId"], "s"), session))
        if order_session:
            wanted = {order_session}
        available_ids = {x[0] for x in visible}
        if wanted - available_ids:
            # Don't cache a stale list after detecting a requested session is missing.
            self.transport.store.discard_metadata(
                event_id, owner=self.transport.owner, clock=self.transport.clock
            )
            raise SourceError(
                "PARSE" if cached else "UNSUPPORTED", "指定場次不在此活動的公開場次清單"
            )
        if len(available_ids) != len(visible) or not visible:
            raise SourceError("PARSE", "活動沒有可辨識的公開場次，或 ID 重複")
        if len(visible) > 100:
            raise SourceError("UNSUPPORTED", "活動場次數超過目前單次批次查詢上限")
        if order_session:
            session = next(session for ident, session in visible if ident == order_session)
            return await self._inventory(
                target, event_public_id, order_session, metadata, session, before
            )
        data = await self.transport.get_json(
            STATUS_API, {"eventId": event_id, "sessionId": ",".join(sorted(available_ids))}
        )
        if str(data.get("errCode")) != "00":
            raise SourceError("PARSE", "票況 API 回報錯誤")
        result = data.get("result")
        rows = result.get("session") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            raise SourceError("PARSE", "票況 API 未提供場次資料")
        statuses, raw_statuses = {}, {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or row["id"] in statuses
            ):
                raise SourceError("PARSE", "票況場次 ID 缺失或重複")
            raw_status = row.get("status")
            raw_statuses[row["id"]] = raw_status if isinstance(raw_status, str) else None
            statuses[row["id"]] = (
                STATUS_MAP.get(raw_status, TicketStatus.UNKNOWN)
                if isinstance(raw_status, str)
                else TicketStatus.UNKNOWN
            )
        items = []
        for ident, session in visible:
            if wanted and ident not in wanted:
                continue
            items.append(
                TicketItem(
                    ident,
                    ident,
                    str(session.get("name") or metadata["title"]),
                    statuses.get(ident, TicketStatus.UNKNOWN),
                    url,
                    session.get("date"),
                    session.get("time"),
                    session.get("location"),
                    source_status=raw_statuses.get(ident),
                    availability_text=(
                        "暫無票券" if raw_statuses.get(ident) == "unavailable" else None
                    ),
                    session_name=str(session.get("name") or metadata["title"]),
                    order_url=f"https://ticketplus.com.tw/order/{event_public_id}/{public_id(ident)}",
                )
            )
        return Observation(
            metadata["title"],
            url,
            self.transport.clock(),
            tuple(items),
            source="ticketplus-public-v2/session",
            complete=all(x.status != TicketStatus.UNKNOWN for x in items),
            incomplete_session_ids=frozenset(
                x.session_id for x in items if x.status == TicketStatus.UNKNOWN
            ),
            request_count=self.transport.count - before,
            session_starts={
                ident: session_start(session.get("date"), session.get("time"))
                for ident, session in visible
                if not wanted or ident in wanted
            },
        )

    async def _inventory(
        self,
        target: Target,
        event_public_id: str,
        session_id: str,
        metadata: dict,
        session: dict,
        before: int,
    ) -> Observation:
        # Follow the site's order-page data flow. Seated events expose area inventory;
        # events without areas expose product (ticket-type) inventory.
        is_area = session.get("ticketArea")
        if not isinstance(is_area, bool):
            raise SourceError("PARSE", "場次未提供可辨識的票區模式")
        kind = "area" if is_area else "product"
        collection = "ticketAreas" if is_area else "products"
        ident_field = "ticketAreaId" if is_area else "productId"
        response_field = "ticketArea" if is_area else "product"
        prefix = "a" if is_area else "p"
        cache_key = f"{kind}/{event_public_id}"
        static = self.transport.store.metadata(cache_key, self.transport.clock())
        cached = static is not None
        if static is None:
            static = await self.transport.get_json(
                STATIC_API, {"path": f"event/{event_public_id}/{collection}.json"}
            )
            if not isinstance(static.get(collection), list):
                raise SourceError("PARSE", "票區或票種靜態資料結構改變")
            self.transport.store.cache_metadata(
                cache_key,
                static,
                self.transport.clock() + 3600,
                owner=self.transport.owner,
                clock=self.transport.clock,
            )
        visible = {}
        for row in static[collection]:
            if not isinstance(row, dict) or not isinstance(row.get("sessionId"), str):
                raise SourceError("PARSE", "票區或票種缺少場次 ID")
            if internal_id(row["sessionId"], "s") != session_id or row.get("hidden") is True:
                continue
            if not isinstance(row.get(ident_field), str):
                raise SourceError("PARSE", "票區或票種缺少穩定 ID")
            ident = internal_id(row[ident_field], prefix)
            if ident in visible:
                raise SourceError("PARSE", "票區或票種 ID 重複")
            visible[ident] = row
        wanted = {internal_id(x, prefix) for x in target.item_ids}
        if wanted - set(visible):
            self.transport.store.discard_metadata(
                cache_key, owner=self.transport.owner, clock=self.transport.clock
            )
            raise SourceError("PARSE" if cached else "UNSUPPORTED", "指定項目不在此場次的公開清單")
        if not visible:
            raise SourceError("PARSE", "場次沒有可辨識的公開票區或票種")
        if len(visible) > 1000:
            raise SourceError("UNSUPPORTED", "場次項目數超過目前完整查詢上限")
        # Validate against the full static list, then fetch every selected item.
        # Output pagination never limits the inventory that gets evaluated.
        rows = {}
        ids = sorted(wanted or visible)
        for start in range(0, len(ids), 100):
            data = await self.transport.get_json(
                STATUS_API, {ident_field: ",".join(ids[start : start + 100])}
            )
            result = data.get("result")
            batch = result.get(response_field) if isinstance(result, dict) else None
            if str(data.get("errCode")) != "00" or not isinstance(batch, list) or not batch:
                raise SourceError("PARSE", "票區或票種 API 未提供有效資料")
            for row in batch:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("id"), str)
                    or row["id"] in rows
                ):
                    raise SourceError("PARSE", "動態票區或票種 ID 缺失或重複")
                rows[row["id"]] = row
        order_url = f"https://ticketplus.com.tw/order/{event_public_id}/{public_id(session_id)}"
        items = []
        for ident, item in visible.items():
            if wanted and ident not in wanted:
                continue
            live = rows.get(ident, {})
            # A row from another session is not reliable inventory for this item.
            if live.get("sessionId") not in (None, session_id):
                raise SourceError("PARSE", "動態票況的場次 ID 不一致")
            status, text, remaining = inventory_status(live, is_area=is_area)
            price = item.get("price")
            items.append(
                TicketItem(
                    ident,
                    session_id,
                    str(item.get("name") or ident),
                    status,
                    order_url,
                    session.get("date"),
                    session.get("time"),
                    session.get("location"),
                    price=price if isinstance(price, int) and not isinstance(price, bool) else None,
                    location=str(item.get("name") or ident) if is_area else None,
                    source_status=live.get("status")
                    if isinstance(live.get("status"), str)
                    else None,
                    availability_text=text,
                    remaining_count=remaining,
                    session_name=str(session.get("name") or metadata["title"]),
                    order_url=order_url,
                )
            )
        return Observation(
            metadata["title"],
            order_url,
            self.transport.clock(),
            tuple(items),
            source=f"ticketplus-public-v1/{kind}",
            granularity="AREA" if is_area else "PRODUCT",
            complete=all(x.status != TicketStatus.UNKNOWN for x in items),
            incomplete_session_ids=frozenset(
                x.session_id for x in items if x.status == TicketStatus.UNKNOWN
            ),
            request_count=self.transport.count - before,
            session_starts={session_id: session_start(session.get("date"), session.get("time"))},
        )


def inventory_status(row: dict, *, is_area: bool) -> tuple[TicketStatus, str | None, int | None]:
    raw = row.get("status")
    if not isinstance(raw, str):
        return TicketStatus.UNKNOWN, None, None
    if row.get("hidden") is True:
        return TicketStatus.PAUSED, "未公開販售", None
    if raw == "soldout":
        return TicketStatus.SOLD_OUT, "已售完", 0
    if raw == "unavailable":
        return TicketStatus.TEMPORARILY_UNAVAILABLE, "暫無票券", 0
    if raw != "onsale":
        return STATUS_MAP.get(raw, TicketStatus.UNKNOWN), None, None
    limited = row.get("ticketAreaLimit" if is_area else "productLimit")
    if limited is False:
        return TicketStatus.AVAILABLE, "熱賣中", None
    count = row.get("count")
    if limited is not True or isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return TicketStatus.UNKNOWN, None, None
    if count == 0:
        return TicketStatus.SOLD_OUT, "已售完", 0
    # Match the page: only counts up to 20 are displayed as remaining tickets.
    # Higher counts (including the observed 999999 value) are shown as hot sale.
    if count <= 20:
        return TicketStatus.AVAILABLE, f"剩餘 {count}", count
    return TicketStatus.AVAILABLE, "熱賣中", None
