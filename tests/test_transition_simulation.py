"""Offline TicketPlus responses through parser, state machine and mock Discord."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime

import pytest
from test_notifications import enable
from test_ticketplus_cases import ORDER, SALE, CaseTransport

from ticket_watcher.config import Target
from ticket_watcher.platforms.ticketplus import TicketPlusAdapter


@pytest.mark.parametrize("outer", [True, False], ids=["activity", "order"])
@pytest.mark.parametrize("via_unavailable", [True, False], ids=["via-unavailable", "direct"])
def test_real_parser_transition_simulation(harness, monkeypatch, outer, via_unavailable):
    h = harness
    h["clock"].now = datetime.fromisoformat("2026-10-06T12:00:00+08:00").timestamp()
    url = f"https://ticketplus.com.tw/activity/{SALE}" if outer else ORDER
    target = Target("test", "模擬票況變化", url, item_ids=() if outer else ("p000017500",))
    h["watcher"].config = replace(h["config"], targets=(target,))
    h["watcher"].notifier.config = h["watcher"].config

    class SimulationTransport(CaseTransport):
        status = "onsale"

        async def get_json(self, url, params):
            response = await super().get_json(url, params)
            for row in response.get("result", {}).get("session", []):
                row["status"] = self.status
            return response

    transport = SimulationTransport(h, SALE)
    product = next(
        row for row in transport.case["live"]["s000002256"]["product"] if row["id"] == "p000017500"
    )
    h["watcher"].adapter = TicketPlusAdapter(transport)
    enable(h, monkeypatch)

    def observe(status):
        transport.status = status
        product.update(status=status, count=1 if status == "onsale" else 0)
        state = h["watcher"].store.target("test")
        if state:
            h["clock"].now = max(h["clock"].now, state["next_check"])
        return asyncio.run(h["watcher"].check("test"))

    # Adding an already available target and polling it again must remain quiet.
    observe("onsale")
    observe("onsale")
    assert not h["requests"]
    observe("soldout")
    assert not h["requests"]

    if via_unavailable:
        result = observe("unavailable")
        assert result.data["evaluation"]["release_hint_detected"] is outer
        assert not result.data["evaluation"]["release_detected"]
        assert len(h["requests"]) == int(outer)
        observe("unavailable")
        assert len(h["requests"]) == int(outer)

    result = observe("onsale")
    assert result.data["evaluation"]["release_detected"]
    assert result.data["notification"]["status"] == "SENT"
    expected = 1 + int(outer and via_unavailable)
    assert len(h["requests"]) == expected
    observe("onsale")
    assert len(h["requests"]) == expected
    if outer and via_unavailable:
        assert "釋票線索" in json.loads(h["requests"][0].content)["content"]
    assert "釋票線索" not in json.loads(h["requests"][-1].content)["content"]
