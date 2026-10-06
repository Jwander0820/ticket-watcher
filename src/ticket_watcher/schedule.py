"""Monitoring deadlines from verified TicketPlus session date/time fields."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import timestamp


def session_start(date, time) -> float | None:
    # TicketPlus uses either a single value or 'start ~ end'. The start is
    # interpreted in the provider's timezone, not the user's display timezone.
    if not isinstance(date, str) or not isinstance(time, str):
        return None
    dates, times = (
        [part.strip() for part in date.split("~")],
        [part.strip() for part in time.split("~")],
    )
    if (
        len(dates) not in (1, 2)
        or len(times) not in (1, 2)
        or any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", part) for part in dates)
        or any(not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", part) for part in times)
    ):
        return None
    try:
        value = datetime.fromisoformat(f"{dates[0]}T{times[0]}")
        # Validate all supplied endpoints; ambiguous/malformed data must not stop a monitor.
        end = datetime.fromisoformat(f"{dates[-1]}T{times[-1]}")
        if end < value:
            return None
        return value.replace(tzinfo=ZoneInfo("Asia/Taipei")).timestamp()
    except ValueError:
        return None


def stop_info(target, schedule, now) -> dict:
    automatic = schedule["stop_at"] if target.auto_stop and schedule else None
    deadlines = [
        (at, reason)
        for at, reason in (
            (target.stop_at, "STOP_AT"),
            (automatic, "SHOW_STARTED"),
        )
        if at is not None
    ]
    effective, reason = min(deadlines) if deadlines else (None, None)
    return {
        "auto_stop_at": timestamp(automatic),
        "effective_stop_at": timestamp(effective),
        "stop_reason": reason if effective is not None and effective <= now else None,
        "stopped_session_count": len(stopped_sessions(target, schedule, now)),
    }


def stopped_sessions(target, schedule, now) -> set[str]:
    if not target.auto_stop or not schedule:
        return set()
    return {
        ident for ident, start in schedule["sessions"].items() if start is not None and start <= now
    }
