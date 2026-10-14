import asyncio
from dataclasses import replace

import pytest
from conftest import URL
from test_web import mutate, panel, snapshot

from ticket_watcher.models import Observation, TicketItem, TicketStatus


async def add_target(client):
    response = await mutate(
        client, await snapshot(client), "/api/targets", {"name": "票區預覽", "url": URL}
    )
    assert response.status == 200
    return (await response.json())["targets"][0]["id"]


def test_ui_item_details_paginate_cached_items_without_querying(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            ident = await add_target(client)
            target = c.config.targets[0]
            c.watcher.store.sync_target(target)
            items = tuple(
                TicketItem(
                    f"area-{i:03d}",
                    "session-1",
                    f"票區 {i}",
                    TicketStatus.SOLD_OUT,
                    URL,
                    date="2026-11-29",
                    time="19:00",
                    venue="範例場館",
                    price=3200,
                    location=f"座位區 {i}",
                    session_name="台北場",
                )
                for i in range(53)
            )
            c.watcher._apply(
                target,
                Observation("測試活動", URL, c.watcher.clock(), items, granularity="AREA"),
            )
            before = c.watcher.store.items(ident)
            response = await client.get(f"/api/targets/{ident}/items?limit=1000")
            assert response.status == 200
            page = await response.json()
            assert page["total"] == 53 and page["next_offset"] == 50
            assert len(page["items"]) == 50
            assert page["items"][0]["name"] == "票區 0"
            assert page["items"][0]["price"] == 3200
            assert page["items"][0]["location"] == "座位區 0"
            assert page["items"][0]["session_name"] == "台北場"
            assert page["items"][0]["status"] == "SOLD_OUT"
            assert page["items"][0]["last_valid_status"] == "SOLD_OUT"
            assert response.headers["Cache-Control"] == "no-store"
            page = await (await client.get(f"/api/targets/{ident}/items?offset=50")).json()
            assert [item["item_key"] for item in page["items"]] == [
                "area-050",
                "area-051",
                "area-052",
            ]
            assert page["total"] == 53 and page["next_offset"] is None
            page = await (await client.get(f"/api/targets/{ident}/items?offset=53")).json()
            assert page == {"items": [], "total": 53, "next_offset": None}
            assert "page" not in (await snapshot(client))["targets"][0]["state"]
            assert c.watcher.store.items(ident) == before and not requests

    asyncio.run(scenario())


def test_ui_item_details_preserve_last_valid_status_for_partial_observation(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            ident = await add_target(client)
            target = c.config.targets[0]
            c.watcher.store.sync_target(target)
            item = TicketItem("area-1", "session-1", "紅區", TicketStatus.AVAILABLE, URL)
            observed_at = c.watcher.clock()
            c.watcher._apply(target, Observation("測試", URL, observed_at, (item,)))
            c.watcher._apply(
                target,
                Observation(
                    "測試",
                    URL,
                    observed_at + 1,
                    (replace(item, status=TicketStatus.UNKNOWN),),
                    complete=False,
                ),
            )
            response = await client.get(f"/api/targets/{ident}/items")
            assert response.status == 200
            cached = (await response.json())["items"][0]
            assert cached["status"] == "UNKNOWN" and cached["last_valid_status"] == "AVAILABLE"
            assert cached["observed_at"] != cached["last_valid_at"]
            assert not requests

    asyncio.run(scenario())


def test_ui_item_details_empty_before_first_observation(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            ident = await add_target(client)
            response = await client.get(f"/api/targets/{ident}/items")
            assert response.status == 200
            assert await response.json() == {"items": [], "total": 0, "next_offset": None}
            assert c.watcher.store.target(ident) is None and not requests

    asyncio.run(scenario())


@pytest.mark.parametrize("offset", ["-1", "1.5", "abc", "", "+1", "9223372036854775808"])
def test_ui_item_details_reject_invalid_offsets(tmp_path, offset):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            ident = await add_target(client)
            response = await client.get(f"/api/targets/{ident}/items", params={"offset": offset})
            assert response.status == 400 and not requests

    asyncio.run(scenario())


def test_ui_item_details_reject_unknown_target_and_cross_site_reads(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            ident = await add_target(client)
            assert (await client.get("/api/targets/missing/items")).status == 404
            for headers in (
                {"Host": "attacker.invalid"},
                {"Origin": "https://attacker.invalid"},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                response = await client.get(f"/api/targets/{ident}/items", headers=headers)
                assert response.status == 403
            assert not requests

    asyncio.run(scenario())
