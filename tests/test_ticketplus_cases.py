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
    """Recorded public responses, with batch behavior matching the public API."""

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
    ) == ("SOLD_OUT", "暫無票券", 0)


@pytest.mark.parametrize("selected", [0, 1, 110])
def test_more_than_100_areas_are_batched_without_truncation(harness, selected):
    transport = CaseTransport(harness, YUURI)
    sid = transport.case["sessions"][0]["sessionId"]
    internal = internal_id(sid, "s")
    static = next(x for x in transport.case["ticketAreas"] if x["sessionId"] == sid)
    dynamic = transport.case["live"][internal]["ticketArea"][0]
    for i in range(110):
        ident = f"a{900000000 + i:09d}"
        transport.case["ticketAreas"].append({**static, "ticketAreaId": ident})
        transport.case["live"][internal]["ticketArea"].append({**dynamic, "id": ident})
    filters = tuple(f"a{900000000 + i:09d}" for i in range(selected))
    result = asyncio.run(
        TicketPlusAdapter(transport).fetch(
            Target(
                "test", "test", f"https://ticketplus.com.tw/order/{YUURI}/{sid}", item_ids=filters
            )
        )
    )
    assert result.complete and len(result.items) == (selected or 181)
    if selected:
        assert {item.item_key for item in result.items} == set(filters)
    assert [
        len(x["ticketAreaId"].split(",")) for x in transport.requests if "ticketAreaId" in x
    ] == {0: [100, 81], 1: [1], 110: [100, 10]}[selected]


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
    assert "票種" in text and "身障票" in text and "剩餘 1" in text and "250" in text


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
    assert "釋票線索" in text and "未確認正數餘票" in text
    assert not check(harness, "TEMPORARILY_UNAVAILABLE").data["evaluation"]["release_hint_detected"]
    confirmed = check(harness, "AVAILABLE")
    assert (
        confirmed.data["evaluation"]["release_detected"]
        and not confirmed.data["evaluation"]["release_hint_detected"]
    )
    assert len(harness["requests"]) == 2


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
