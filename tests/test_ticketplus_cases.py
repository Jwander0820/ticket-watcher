import asyncio
import copy
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from test_monitoring import check
from test_notifications import enable

from ticket_watcher.config import Target, load_config, validate_url
from ticket_watcher.models import SourceError
from ticket_watcher.platforms.ticketplus import TicketPlusAdapter, internal_id, inventory_status

YUURI = "7acedf4b414903ac17104384cb416849"
SALE = "6def673ab73ab7d7a4d0597ae84809ca"
ORDER = f"https://ticketplus.com.tw/order/{SALE}/c2e30fcb68cdcd8cee2b3576dd4fe2f5"
CASES = json.loads(
    (Path(__file__).parent / "fixtures/ticketplus-cases.json").read_text(encoding="utf-8")
)


class CaseTransport:
    """Recorded samples with synthetic ID filtering, not proof of live API limits."""

    owner = None  # Parser-only fixture; no HTTP request/lease is exercised.

    def __init__(self, harness, event):
        self.store = harness["watcher"].store
        self.clock = harness["clock"]
        self.count = 0
        self.event = event
        self.case = copy.deepcopy(CASES[event])
        self.requests = []

    async def get_json(self, url, params):
        self.count += 1
        self.requests.append(params)
        if "path" in params:
            filename = params["path"].rsplit("/", 1)[1]
            if filename == "event.json":
                return {"title": self.case["title"]}
            collection = filename.removesuffix(".json")
            return {collection: self.case[collection]}
        result = {}
        if "sessionId" in params:
            result["session"] = [
                {"id": sid, "status": "soldout" if self.event == YUURI else "onsale"}
                for sid in params["sessionId"].split(",")
            ]
        for parameter, collection in (("productId", "product"), ("ticketAreaId", "ticketArea")):
            if parameter in params:
                wanted = set(params[parameter].split(","))
                result[collection] = [
                    row
                    for live in self.case["live"].values()
                    for row in live[collection]
                    if row["id"] in wanted
                ]
        return {"errCode": "00", "result": result}


def fetch(harness, event, url, **filters):
    transport = CaseTransport(harness, event)
    result = asyncio.run(TicketPlusAdapter(transport).fetch(Target("test", "test", url, **filters)))
    return result, transport


def test_yuuri_outer_only_discovers_both_soldout_sessions_and_order_links(harness):
    result, _ = fetch(harness, YUURI, f"https://ticketplus.com.tw/activity/{YUURI}")
    assert result.complete and len(result.items) == 2 and result.granularity == "SESSION"
    assert {x.status for x in result.items} == {"SOLD_OUT"}
    assert {x.date for x in result.items} == {"2026-10-09 ~ 2026-10-09", "2026-10-10 ~ 2026-10-10"}
    assert all(
        x.order_url.startswith(f"https://ticketplus.com.tw/order/{YUURI}/") for x in result.items
    )


@pytest.mark.parametrize(
    "sid", ["c88be5c1c01bf684e9a56b6defcd6e8d", "2606691b5fcc73c52940bfb047a3d622"]
)
def test_yuuri_order_evaluates_all_71_areas_even_when_page_has_no_buy_button(harness, sid):
    result, transport = fetch(harness, YUURI, f"https://ticketplus.com.tw/order/{YUURI}/{sid}")
    assert result.complete and len(result.items) == 71 and result.granularity == "AREA"
    assert all(x.status == "SOLD_OUT" and x.remaining_count == 0 for x in result.items)
    assert any(x.price == 5280 and x.location == "特A區5280" for x in result.items)
    assert len(transport.requests[-1]["ticketAreaId"].split(",")) == 71
    assert "productId" not in transport.requests[-1]


def test_sale_order_preserves_ticket_type_and_matches_visible_count(harness):
    result, _ = fetch(harness, SALE, ORDER)
    assert result.complete and result.granularity == "PRODUCT"
    items = {x.item_key: x for x in result.items}
    assert items["p000017499"].name == "全票"
    assert items["p000017499"].status == "AVAILABLE"
    assert items["p000017499"].remaining_count is None
    assert items["p000017499"].availability_text == "熱賣中"
    assert items["p000017500"].name == "身障票"
    assert items["p000017500"].remaining_count == 1 and items["p000017500"].price == 250


def test_order_item_filter_fetches_only_selected_ticket_type(harness):
    result, transport = fetch(harness, SALE, ORDER, item_ids=("p000017499",))
    assert len(result.items) == 1 and result.items[0].name == "全票"
    assert result.complete
    assert transport.requests[-1]["productId"] == "p000017499"


def test_order_rejects_conflicting_session_filter_without_http(harness):
    transport = CaseTransport(harness, SALE)
    with pytest.raises(SourceError) as error:
        asyncio.run(
            TicketPlusAdapter(transport).fetch(
                Target("test", "test", ORDER, session_ids=("s000002093",))
            )
        )
    assert error.value.code == "UNSUPPORTED" and transport.count == 0


def test_order_missing_dynamic_item_is_unknown_not_soldout(harness):
    transport = CaseTransport(harness, SALE)
    transport.case["live"]["s000002256"]["product"] = [
        x for x in transport.case["live"]["s000002256"]["product"] if x["id"] != "p000017500"
    ]
    result = asyncio.run(TicketPlusAdapter(transport).fetch(Target("test", "test", ORDER)))
    assert not result.complete
    assert next(x for x in result.items if x.item_key == "p000017500").status == "UNKNOWN"


@pytest.mark.parametrize("is_area", [False, True])
@pytest.mark.parametrize(
    "count,status,text,remaining",
    [
        (0, "SOLD_OUT", "已售完", 0),
        (1, "AVAILABLE", "剩餘 1", 1),
        (20, "AVAILABLE", "剩餘 20", 20),
        (21, "AVAILABLE", "熱賣中", None),
        (999999, "AVAILABLE", "熱賣中", None),
        (None, "UNKNOWN", None, None),
        (-1, "UNKNOWN", None, None),
        (True, "UNKNOWN", None, None),
    ],
)
def test_inventory_count_matches_page_threshold_and_rejects_bad_data(
    is_area, count, status, text, remaining
):
    key = "ticketAreaLimit" if is_area else "productLimit"
    assert inventory_status({"status": "onsale", key: True, "count": count}, is_area=is_area) == (
        status,
        text,
        remaining,
    )


def test_unavailable_inventory_cannot_report_positive_count():
    assert inventory_status(
        {"status": "unavailable", "productLimit": True, "count": 1}, is_area=False
    ) == ("TEMPORARILY_UNAVAILABLE", "暫無票券", 0)


def large_inventory_case(harness, event):
    transport = CaseTransport(harness, event)
    sid = transport.case["sessions"][0]["sessionId"]
    internal = internal_id(sid, "s")
    collection, field, prefix = (
        ("ticketAreas", "ticketArea", "a") if event == YUURI else ("products", "product", "p")
    )
    parameter = f"{field}Id"
    static = next(x for x in transport.case[collection] if x["sessionId"] == sid)
    dynamic = transport.case["live"][internal][field][0]
    transport.case[collection] = [
        {**static, parameter: f"{prefix}{900000000 + i:09d}"} for i in range(132)
    ]
    # Reverse the response and vary inventory to require matching by ID, not index.
    live = [
        {
            **dynamic,
            "id": f"{prefix}{900000000 + i:09d}",
            "status": "onsale" if i % 2 else "soldout",
            f"{field}Limit": True,
            "count": 1 if i % 2 else 0,
        }
        for i in reversed(range(132))
    ]
    transport.case["live"][internal][field] = live
    target = Target("test", "test", f"https://ticketplus.com.tw/order/{event}/{sid}")
    return transport, target, parameter, internal, live


@pytest.mark.parametrize("event", [YUURI, SALE])
@pytest.mark.parametrize("selected", [0, 1, 110])
def test_more_than_100_items_use_one_dynamic_request_with_all_ids(harness, event, selected):
    transport, target, parameter, _, live = large_inventory_case(harness, event)
    filters = tuple(sorted(row["id"] for row in live)[:selected])
    target = replace(target, item_ids=filters)
    adapter = TicketPlusAdapter(transport)
    result = asyncio.run(adapter.fetch(target))
    assert result.complete and len(result.items) == (selected or 132)
    assert result.request_count == 4  # Three static resources plus one inventory request.
    expected_ids = set(filters) if selected else {row["id"] for row in live}
    assert {item.item_key for item in result.items} == expected_ids
    for item in result.items:
        assert item.status == ("AVAILABLE" if int(item.item_key[1:]) % 2 else "SOLD_OUT")
    dynamic_requests = [x for x in transport.requests if parameter in x]
    assert len(dynamic_requests) == 1
    assert set(dynamic_requests[0][parameter].split(",")) == expected_ids
    transport.requests.clear()
    cached = asyncio.run(adapter.fetch(target))
    assert cached.complete and cached.items == result.items
    assert cached.request_count == len(transport.requests) == 1
    assert set(transport.requests[0][parameter].split(",")) == expected_ids


@pytest.mark.parametrize("event", [YUURI, SALE])
def test_truncated_large_response_marks_missing_items_unknown_without_extra_requests(
    harness, event
):
    transport, target, parameter, session_id, live = large_inventory_case(harness, event)
    missing = {row["id"] for row in live[100:]}
    del live[100:]
    result = asyncio.run(TicketPlusAdapter(transport).fetch(target))
    assert not result.complete and len(result.items) == 132
    assert result.incomplete_session_ids == {session_id}
    assert {item.item_key for item in result.items if item.status == "UNKNOWN"} == missing
    assert result.request_count == 4
    dynamic_requests = [x for x in transport.requests if parameter in x]
    assert len(dynamic_requests) == 1
    assert len(dynamic_requests[0][parameter].split(",")) == 132


def test_order_product_release_compares_quantity_and_formats_ticket_name(harness, monkeypatch):
    h = harness
    # This fixture performs on October 9; test the release before its automatic stop.
    h["clock"].now = datetime.fromisoformat("2026-10-06T12:00:00+08:00").timestamp()
    target = Target("test", "test", ORDER, item_ids=("p000017500",))
    h["watcher"].config = replace(h["config"], targets=(target,))
    h["watcher"].notifier.config = h["watcher"].config
    transport = CaseTransport(h, SALE)
    adapter = TicketPlusAdapter(transport)
    h["watcher"].adapter = adapter
    product = next(
        x for x in transport.case["live"]["s000002256"]["product"] if x["id"] == "p000017500"
    )
    product["count"] = 0
    assert not asyncio.run(h["watcher"].check("test")).data["evaluation"]["release_detected"]
    h["clock"].advance(300)
    product["count"] = 1
    enable(h, monkeypatch)
    result = asyncio.run(h["watcher"].check("test"))
    assert (
        result.data["evaluation"]["release_detected"]
        and result.data["notification"]["status"] == "SENT"
    )
    text = json.loads(h["requests"][0].content)["content"]
    assert "身障票｜$250｜剩 1 張" in text
    assert text.startswith("🟢 有票｜")


def test_outer_unavailable_hint_is_distinct_deduplicated_and_can_be_confirmed(harness, monkeypatch):
    enable(harness, monkeypatch)
    check(harness, "SOLD_OUT")
    hint = check(harness, "TEMPORARILY_UNAVAILABLE")
    assert hint.data["evaluation"] == {
        "performed": True,
        "release_detected": False,
        "release_hint_detected": True,
    }
    assert hint.data["event_id"] is None and hint.data["hint_event_id"]
    assert hint.data["hint_notification"]["status"] == "SENT"
    text = json.loads(harness["requests"][0].content)["content"]
    assert text.startswith("🟡 暫無票券｜") and "🟢" not in text
    assert not check(harness, "TEMPORARILY_UNAVAILABLE").data["evaluation"]["release_hint_detected"]
    confirmed = check(harness, "AVAILABLE")
    assert (
        confirmed.data["evaluation"]["release_detected"]
        and not confirmed.data["evaluation"]["release_hint_detected"]
    )
    assert len(harness["requests"]) == 2


@pytest.mark.parametrize(
    "granularity,event", [("SESSION", SALE), ("AREA", YUURI), ("PRODUCT", SALE)]
)
def test_first_unavailable_from_real_parser_enables_fast_mode(harness, granularity, event):
    h, w = harness, harness["watcher"]
    h["clock"].now = datetime.fromisoformat("2026-10-06T12:00:00+08:00").timestamp()

    class UnavailableTransport(CaseTransport):
        async def get_json(self, url, params):
            response = await super().get_json(url, params)
            for rows in response.get("result", {}).values():
                for row in rows:
                    row["status"] = "unavailable"
            return response

    transport = UnavailableTransport(h, event)
    url = f"https://ticketplus.com.tw/activity/{event}"
    if granularity != "SESSION":
        url = (
            f"https://ticketplus.com.tw/order/{event}/{transport.case['sessions'][0]['sessionId']}"
        )
    target = Target("test", "test", url)
    w.config = replace(h["config"], targets=(target,))
    w.adapter = TicketPlusAdapter(transport)
    for _ in range(3):
        result = asyncio.run(w.check("test", immediate=True))
        assert result.data["granularity"] == granularity and result.data["complete"]
        assert not result.data["evaluation"]["release_detected"]
        assert not result.data["evaluation"]["release_hint_detected"]
        assert set(result.data["summary"]) == {"TEMPORARILY_UNAVAILABLE"}
        state = w.store.target("test")
        assert state["mode"] == "ACTIVE" and state["no_available"] == 0
        assert state["active_until"] == h["clock"]() + h["config"].active_window
        assert state["next_check"] == h["clock"]() + 60
        h["clock"].now = state["next_check"]
    assert not w.events().data["events"] and not h["requests"]


def test_hint_queue_cancels_when_soldout_returns(harness, monkeypatch):
    check(harness, "SOLD_OUT")
    result = check(harness, "TEMPORARILY_UNAVAILABLE")
    event = result.data["hint_event_id"]
    check(harness, "SOLD_OUT")
    enable(harness, monkeypatch)
    asyncio.run(harness["watcher"].notifier.deliver())
    assert not harness["requests"]
    assert harness["watcher"].store.notification_status(event)["status"] == "CANCELLED"


def test_case_config_uses_shared_database_and_stays_disabled():
    config = load_config("examples/ticketplus-cases.yaml")
    assert all(not x.enabled for x in config.targets)
    assert config.database_path == Path("data/watcher.db").resolve()
    assert all(validate_url(x.url) == x.url for x in config.targets)
