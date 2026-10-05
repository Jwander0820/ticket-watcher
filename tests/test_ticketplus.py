import asyncio

import pytest
from conftest import URL

from ticket_watcher.config import Target
from ticket_watcher.models import SourceError
from ticket_watcher.platforms.ticketplus import (
    STATUS_MAP,
    TicketPlusAdapter,
    internal_id,
    public_id,
)


class SampleTransport:
    def __init__(self, harness, statuses=None):
        self.store = harness["watcher"].store
        self.clock = harness["clock"]
        self.count = 0
        self.requests = []
        self.statuses = (
            statuses if statuses is not None else [{"id": "s000001778", "status": "soldout"}]
        )

    async def get_json(self, url, params):
        self.count += 1
        self.requests.append((url, params))
        if params.get("path", "").endswith("event.json"):
            return {"title": "公開樣本活動"}
        if params.get("path", "").endswith("sessions.json"):
            return {
                "sessions": [
                    {
                        "sessionId": "1d68ecb8d8d6df5f82f416f3c9bb63c1",
                        "name": "場次",
                        "location": "場館",
                        "hidden": False,
                    }
                ]
            }
        return {"errCode": "00", "result": {"session": self.statuses}}


def test_public_id_conversion_matches_verified_frontend():
    assert internal_id("190c8cf3965a985d9151d912313e2fe1", "e") == "e000001189"
    assert internal_id("1d68ecb8d8d6df5f82f416f3c9bb63c1", "s") == "s000001778"
    assert public_id("e000001189") == "190c8cf3965a985d9151d912313e2fe1"


@pytest.mark.parametrize("value", ["bad", "00000000000000000000000000000000", "s000001778"])
def test_invalid_event_ids_are_unsupported(value):
    with pytest.raises(SourceError, match="ID"):
        internal_id(value, "e")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("onsale", "AVAILABLE"),
        ("soldout", "SOLD_OUT"),
        ("pending", "UPCOMING"),
        ("over", "ENDED"),
        ("lock", "PAUSED"),
        ("unavailable", "TEMPORARILY_UNAVAILABLE"),
    ],
)
def test_verified_status_mapping(raw, expected):
    assert STATUS_MAP[raw].value == expected


def test_adapter_fetches_complete_sample_and_only_caches_metadata(harness):
    transport = SampleTransport(harness)
    adapter = TicketPlusAdapter(transport)
    target = Target("test", "test", URL)
    first = asyncio.run(adapter.fetch(target))
    assert first.items[0].status == "SOLD_OUT" and first.complete
    assert first.request_count == 3
    transport.statuses = [{"id": "s000001778", "status": "onsale"}]
    second = asyncio.run(adapter.fetch(target))
    assert second.items[0].status == "AVAILABLE" and second.request_count == 1
    assert transport.requests[-1][1] == {"eventId": "e000001189", "sessionId": "s000001778"}


def test_unknown_source_status_is_unknown_not_sold_out(harness):
    transport = SampleTransport(harness, [{"id": "s000001778", "status": "new_status"}])
    result = asyncio.run(TicketPlusAdapter(transport).fetch(Target("test", "test", URL)))
    assert result.items[0].status == "UNKNOWN" and not result.complete


def test_empty_api_response_is_parse_failure(harness):
    transport = SampleTransport(harness, [])
    with pytest.raises(SourceError) as error:
        asyncio.run(TicketPlusAdapter(transport).fetch(Target("test", "test", URL)))
    assert error.value.code == "PARSE"


def test_item_filter_is_not_silently_dropped(harness):
    transport = SampleTransport(harness)
    with pytest.raises(SourceError) as error:
        asyncio.run(
            TicketPlusAdapter(transport).fetch(Target("test", "test", URL, item_ids=("area-a",)))
        )
    assert error.value.code == "UNSUPPORTED" and transport.count == 0


def test_session_filter_accepts_both_public_and_internal_ids(harness):
    transport = SampleTransport(harness)
    adapter = TicketPlusAdapter(transport)
    for ident in ("s000001778", "1d68ecb8d8d6df5f82f416f3c9bb63c1"):
        result = asyncio.run(adapter.fetch(Target("test", "test", URL, session_ids=(ident,))))
        assert result.items[0].session_id == "s000001778"


def test_missing_requested_session_is_unsupported(harness):
    transport = SampleTransport(harness)
    with pytest.raises(SourceError) as error:
        asyncio.run(
            TicketPlusAdapter(transport).fetch(
                Target("test", "test", URL, session_ids=("s000000001",))
            )
        )
    assert error.value.code == "UNSUPPORTED"


def test_malformed_status_is_unknown_instead_of_crashing(harness):
    transport = SampleTransport(harness, [{"id": "s000001778", "status": {"unexpected": True}}])
    result = asyncio.run(TicketPlusAdapter(transport).fetch(Target("test", "test", URL)))
    assert result.items[0].status == "UNKNOWN" and not result.complete
