import re

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..config import Target, validate_url
from ..models import Observation, SourceError, TicketItem, TicketStatus
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
    "unavailable": TicketStatus.PAUSED,
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
        if target.item_ids:
            raise SourceError("UNSUPPORTED", "目前來源僅驗證場次票況，無法套用票區或票種 item_ids")
        event_id = internal_id(url.rsplit("/", 1)[1], "e")
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
            self.transport.store.cache_metadata(event_id, metadata, self.transport.clock() + 3600)
        visible = []
        for session in metadata["sessions"]:
            if not isinstance(session, dict):
                raise SourceError("PARSE", "場次資料結構改變")
            if session.get("hidden") is True:
                continue
            if not isinstance(session.get("sessionId"), str):
                raise SourceError("PARSE", "場次缺少穩定 ID")
            visible.append((internal_id(session["sessionId"], "s"), session))
        wanted = {internal_id(x, "s") for x in target.session_ids}
        available_ids = {x[0] for x in visible}
        if wanted - available_ids:
            # Don't cache a stale list after detecting a requested session is missing.
            self.transport.store.connection.execute("DELETE FROM metadata WHERE key=?", (event_id,))
            raise SourceError(
                "PARSE" if cached else "UNSUPPORTED", "指定場次不在此活動的公開場次清單"
            )
        if len(available_ids) != len(visible) or not visible:
            raise SourceError("PARSE", "活動沒有可辨識的公開場次，或 ID 重複")
        if len(visible) > 100:
            raise SourceError("UNSUPPORTED", "活動場次數超過目前單次批次查詢上限")
        data = await self.transport.get_json(
            STATUS_API, {"eventId": event_id, "sessionId": ",".join(sorted(available_ids))}
        )
        if str(data.get("errCode")) != "00":
            raise SourceError("PARSE", "票況 API 回報錯誤")
        result = data.get("result")
        rows = result.get("session") if isinstance(result, dict) else None
        if not isinstance(rows, list) or not rows:
            raise SourceError("PARSE", "票況 API 未提供場次資料")
        statuses = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or row["id"] in statuses
            ):
                raise SourceError("PARSE", "票況場次 ID 缺失或重複")
            raw_status = row.get("status")
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
                )
            )
        return Observation(
            metadata["title"],
            url,
            self.transport.clock(),
            tuple(items),
            complete=all(x.status != TicketStatus.UNKNOWN for x in items),
            request_count=self.transport.count - before,
        )
