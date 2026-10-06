from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TicketStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    UPCOMING = "UPCOMING"
    AVAILABLE = "AVAILABLE"
    SOLD_OUT = "SOLD_OUT"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


def utcnow() -> float:
    return datetime.now(UTC).timestamp()


def timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


@dataclass(frozen=True)
class TicketItem:
    item_key: str
    session_id: str
    name: str
    status: TicketStatus
    public_url: str
    date: str | None = None
    time: str | None = None
    venue: str | None = None
    price: int | None = None
    location: str | None = None
    source_status: str | None = None
    availability_text: str | None = None
    remaining_count: int | None = None
    session_name: str | None = None
    order_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Observation:
    event_name: str
    public_url: str
    observed_at: float
    items: tuple[TicketItem, ...]
    source: str = "ticketplus-public-v2/session"
    granularity: str = "SESSION"
    complete: bool = True
    request_count: int = 0
    session_starts: dict[str, float | None] | None = None
    # When provided, these sessions account for all source incompleteness.
    # None keeps an adapter's unspecified partial result conservative.
    incomplete_session_ids: frozenset[str] | None = None


class SourceError(Exception):
    """Safe diagnostics only: never include bodies, headers or credential URLs."""

    def __init__(self, code: str, message: str, retry_after: float = 0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass
class Result:
    execution_status: str = "COMPLETED"
    result_source: str = "LOCAL"
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "execution_status": self.execution_status,
            "result_source": self.result_source,
            **self.data,
        }
