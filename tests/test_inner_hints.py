"""Inner-page hint transitions through the real parser and mock Discord."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime

import pytest
from test_notifications import enable
from test_ticketplus_cases import SALE, YUURI, CaseTransport

from ticket_watcher.config import Target
from ticket_watcher.platforms.ticketplus import TicketPlusAdapter


@pytest.fixture(params=["AREA", "PRODUCT"])
def inner_monitor(harness, request):
    h, w = harness, harness["watcher"]
    h["clock"].now = datetime.fromisoformat("2026-10-06T12:00:00+08:00").timestamp()
    area = request.param == "AREA"
    event = YUURI if area else SALE
    transport = CaseTransport(h, event)
    session_id = transport.case["sessions"][0]["sessionId"]
    collection, id_field = ("ticketAreas", "ticketAreaId") if area else ("products", "productId")
    item = next(row for row in transport.case[collection] if row["sessionId"] == session_id)
    dynamic = next(
        row
        for live in transport.case["live"].values()
        for row in live["ticketArea" if area else "product"]
        if row["id"] == item[id_field]
    )
    target = Target(
        "test",
        "Inner hint test",
        f"https://ticketplus.com.tw/order/{event}/{session_id}",
        item_ids=(item[id_field],),
    )
    w.config = replace(h["config"], targets=(target,))
    w.notifier.config = w.config
    w.adapter = TicketPlusAdapter(transport)

    def observe(raw):
        dynamic.update(status=raw, count=1 if raw == "onsale" else 0)
        dynamic["ticketAreaLimit" if area else "productLimit"] = True
        state = w.store.target("test")
        if state:
            h["clock"].now = max(h["clock"](), state["next_check"])
        return asyncio.run(w.check("test"))

    return h, item, request.param, observe


def test_inner_hint_sends_location_deduplicates_and_can_be_confirmed(inner_monitor, monkeypatch):
    h, item, granularity, observe = inner_monitor
    enable(h, monkeypatch)
    assert not observe("soldout").data["evaluation"]["release_hint_detected"]
    result = observe("unavailable")
    assert result.data["evaluation"] == {
        "performed": True,
        "release_detected": False,
        "release_hint_detected": True,
    }
    assert result.data["hint_notification"]["status"] == "SENT"
    assert result.data["notification"]["status"] == "NOT_REQUIRED"
    event = next(
        e for e in h["watcher"].events(detail=True).data["events"] if e["kind"] == "RELEASE_HINT"
    )
    assert event["payload"]["granularity"] == granularity
    assert event["payload"]["changes"][0]["current"] == "TEMPORARILY_UNAVAILABLE"
    content = json.loads(h["requests"][0].content)["content"]
    name = "特A區" if granularity == "AREA" else "全票"
    assert content.startswith("🟡 暫無票券｜")
    assert f"{name}｜${item['price']:,}｜暫無票券" in content
    assert "[前往購票](https://ticketplus.com.tw/order/" in content
    assert content.count("https://") == 1
    assert "外頁" not in content
    for _ in range(3):
        assert not observe("unavailable").data["evaluation"]["release_hint_detected"]
    assert len(h["requests"]) == 1
    result = observe("onsale")
    assert result.data["evaluation"]["release_detected"]
    assert result.data["notification"]["status"] == "SENT" and len(h["requests"]) == 2
    assert json.loads(h["requests"][1].content)["content"].startswith("🟢 有票｜")
    observe("soldout")
    assert observe("unavailable").data["evaluation"]["release_hint_detected"]
    assert len(h["requests"]) == 3


@pytest.mark.parametrize("next_status", ["soldout", "onsale", "lock"])
def test_inner_pending_hint_cancels_when_signal_disappears(inner_monitor, monkeypatch, next_status):
    h, _, _, observe = inner_monitor
    observe("soldout")
    result = observe("unavailable")
    event_id = result.data["hint_event_id"]
    assert event_id and result.data["hint_notification"]["status"] == "PENDING"
    observe(next_status)
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert h["watcher"].store.notification_status(event_id)["status"] == "CANCELLED"
    assert len(h["requests"]) == int(next_status == "onsale")
    assert all("🟡" not in json.loads(r.content)["content"] for r in h["requests"])


def test_inner_unknown_preserves_soldout_and_pending_hint_baselines(inner_monitor, monkeypatch):
    h, _, _, observe = inner_monitor
    observe("soldout")
    assert not observe("unrecognized").data["complete"]
    result = observe("unavailable")
    assert result.data["evaluation"]["release_hint_detected"]
    assert not observe("unrecognized").data["complete"]
    # An unknown observation does not falsely cancel the last valid signal.
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert h["watcher"].store.notification_status(result.data["hint_event_id"])["status"] == "SENT"
    assert len(h["requests"]) == 1


@pytest.mark.parametrize("via_unknown", [False, True])
def test_legacy_unavailable_baseline_does_not_emit_hint_on_upgrade(inner_monitor, via_unknown):
    h, _, _, observe = inner_monitor
    w = h["watcher"]
    observe("unavailable")
    previous = w.store.items("test")[0]
    details = json.loads(previous["details"])
    details["status"] = "SOLD_OUT"
    w.store.connection.execute(
        "UPDATE items SET observed='SOLD_OUT',last_valid='SOLD_OUT',details=? WHERE target_id='test'",
        (json.dumps(details),),
    )
    if via_unknown:
        observe("unrecognized")
        assert w.store.items("test")[0]["last_valid"] == "TEMPORARILY_UNAVAILABLE"
    result = observe("unavailable")
    assert not result.data["evaluation"]["release_hint_detected"]
    assert not any(e["kind"] in {"RELEASE", "RELEASE_HINT"} for e in w.events().data["events"])
    assert not h["requests"]
    assert observe("onsale").data["evaluation"]["release_detected"]
