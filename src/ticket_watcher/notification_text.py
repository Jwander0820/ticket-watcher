"""Compact ticket messages, shared by queue partitioning and final rendering."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

SIGNAL_STATUSES = {"AVAILABLE", "TEMPORARILY_UNAVAILABLE"}


def plain(value, limit=180):
    text = " ".join(str(value or "").split())[:limit]
    return re.sub(r"([\\`*_~|\[\]])", r"\\\1", text)


def short_range(value, *, date=False):
    parts = str(value or "").split(" ~ ")
    if len(set(parts)) == 1:
        parts = parts[:1]
    if date:
        try:
            dates = [datetime.strptime(part, "%Y-%m-%d") for part in parts]
        except ValueError:
            pass
        else:
            if len({part.year for part in dates}) == 1:
                parts = [f"{part.month}/{part.day}" for part in dates]
    return plain(" ~ ".join(parts), 80)


def ticket_line(item):
    price = item.get("price")
    name = item["name"]
    if price is not None:
        # TicketPlus often appends the price to the area name. Remove only an
        # exact matching suffix, never arbitrary digits in a section name.
        suffix = rf"(?<!\d)(?:NT\$|\$)?(?:{price}|{price:,})(?:元)?$"
        shortened = re.sub(suffix, "", name).strip()
        name = shortened or name
    if item["status"] == "TEMPORARILY_UNAVAILABLE":
        availability = "暫無票券"
    elif item.get("remaining_count") is not None and 0 < item["remaining_count"] <= 20:
        availability = f"剩 {item['remaining_count']} 張"
    else:
        availability = item.get("availability_text") or "可購買"
    fields = [plain(name, 120)]
    if price is not None:
        fields.append(f"${price:,}")
    fields.append(plain(availability, 80))
    return "｜".join(fields)


def ticket_content(payload, items, timezone, *, session_items=None):
    session_items = session_items or items
    first = session_items[0]
    available = any(item["status"] == "AVAILABLE" for item in session_items)
    label = "🟢 有票" if available else "🟡 暫無票券"
    name = plain(payload["event_name"])
    if payload["granularity"] == "SESSION":
        session_name = first.get("session_name") or first["name"]
        if payload["event_name"].casefold() not in session_name.casefold():
            name += " " + plain(session_name, 120)
    when = " ".join(
        value
        for value in (short_range(first.get("date"), date=True), short_range(first.get("time")))
        if value
    )
    title = f"{label}｜{name}" + (f" {when}" if when else "")
    if payload.get("parts", 1) > 1:
        title += f"（{payload['part']}/{payload['parts']}）"
    lines = [title]
    if payload["granularity"] != "SESSION":
        lines.extend(ticket_line(item) for item in items)
    observed = datetime.fromtimestamp(payload["observed_at"], ZoneInfo(timezone))
    lines.append(f"偵測時間：{observed.month}/{observed.day} {observed:%H:%M:%S}")
    lines.append(f"[前往購票]({first.get('order_url') or payload['public_url']})")
    return "\n".join(lines)


def discord_length(content):
    return len(content.encode("utf-16-le")) // 2


def ticket_parts(payload, items, timezone):
    """Partition at row boundaries; every row gets a durable outbox delivery."""
    parts, current = [], []
    for item in items:
        proposed = [*current, item]
        if (
            current
            and discord_length(ticket_content(payload, proposed, timezone, session_items=items))
            > 1900
        ):
            parts.append(current)
            current = []
        current.append(item)
    if current:
        parts.append(current)
    return parts
