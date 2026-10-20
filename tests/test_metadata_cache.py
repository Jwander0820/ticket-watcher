import asyncio
import copy
import json
from dataclasses import replace

import httpx
import pytest
from test_ticketplus_cases import ORDER, SALE, CaseTransport
from test_web import mutate, panel, snapshot

from ticket_watcher.config import Target, parse_config
from ticket_watcher.models import SourceError
from ticket_watcher.platforms.ticketplus import TicketPlusAdapter
from ticket_watcher.storage import Store


def cached_adapter(harness, **config):
    transport = CaseTransport(harness, SALE)
    transport.config = replace(transport.config, **config)
    return TicketPlusAdapter(transport), transport, Target("test", "test", ORDER)


def test_default_seven_days_reuses_static_then_expires(harness):
    adapter, transport, target = cached_adapter(harness)
    assert transport.config.metadata_cache_seconds == 604800
    assert asyncio.run(adapter.fetch(target)).request_count == 4
    harness["clock"].advance(6 * 86400)
    assert asyncio.run(adapter.fetch(target)).request_count == 1
    harness["clock"].advance(86400)
    assert asyncio.run(adapter.fetch(target)).request_count == 4


def test_never_expire_persists_and_policy_changes_use_original_fetch_time(harness):
    adapter, transport, target = cached_adapter(harness, metadata_cache_seconds=0)
    asyncio.run(adapter.fetch(target))
    harness["clock"].advance(30 * 86400)
    transport.store.prune(harness["clock"](), 30, metadata_max_age=0)
    # Read through a separate connection, as on an application restart.
    store = Store(harness["config"].database_path)
    transport.store = store
    try:
        assert asyncio.run(adapter.fetch(target)).request_count == 1
        transport.config = replace(transport.config, metadata_cache_seconds=604800)
        assert asyncio.run(adapter.fetch(target)).request_count == 4
    finally:
        store.close()


def test_new_policy_reuses_legacy_expired_one_hour_cache(harness):
    adapter, transport, target = cached_adapter(harness)
    asyncio.run(adapter.fetch(target))
    now = harness["clock"]()
    db = transport.store.connection
    for row in db.execute("SELECT key,payload FROM metadata").fetchall():
        payload = json.loads(row["payload"])
        payload.pop("_cached_at")
        db.execute(
            "UPDATE metadata SET payload=?,expires_at=? WHERE key=?",
            (json.dumps(payload), now + 3600, row["key"]),
        )
    harness["clock"].advance(2 * 86400)
    transport.store.prune(harness["clock"](), 30, metadata_max_age=604800)
    assert asyncio.run(adapter.fetch(target)).request_count == 1


def test_refresh_only_static_and_next_fetch_uses_updated_shared_cache(harness):
    adapter, transport, target = cached_adapter(harness)
    asyncio.run(adapter.fetch(target))
    transport.requests.clear()
    transport.case["title"] = "更新後活動"
    transport.case["products"][0]["name"] = "更新後票種"
    asyncio.run(adapter.refresh_metadata(target))
    assert len(transport.requests) == 3
    assert all("path" in params for params in transport.requests)
    result = asyncio.run(adapter.fetch(target))
    assert result.request_count == 1 and result.event_name == "更新後活動"
    assert any(item.name == "更新後票種" for item in result.items)


@pytest.mark.parametrize("failure", ["network", "invalid", "cancel"])
def test_failed_refresh_preserves_entire_previous_cache(harness, failure):
    adapter, transport, target = cached_adapter(harness)
    asyncio.run(adapter.fetch(target))
    before = list(transport.store.connection.execute("SELECT * FROM metadata ORDER BY key"))
    before = [tuple(row) for row in before]
    transport.case["title"] = "不應存入"
    original = transport.get_json

    async def fail(url, params):
        if params.get("path", "").endswith("products.json"):
            if failure == "network":
                raise SourceError("NETWORK", "失敗")
            if failure == "cancel":
                raise asyncio.CancelledError
            return {"products": []}
        return await original(url, params)

    transport.get_json = fail
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else SourceError):
        asyncio.run(adapter.refresh_metadata(target))
    after = [
        tuple(row)
        for row in transport.store.connection.execute("SELECT * FROM metadata ORDER BY key")
    ]
    assert after == before


def test_manual_refresh_respects_lease_pause_backoff_and_keeps_ticket_state(harness):
    watcher = harness["watcher"]
    adapter, fixture, target = cached_adapter(harness)
    target = replace(target, auto_stop=False)
    watcher.config = replace(watcher.config, targets=(target,), request_gap=(0, 0))
    watcher.transport.config = watcher.config
    watcher.store.sync_target(target)
    watcher.adapter = TicketPlusAdapter(watcher.transport)
    sent = []

    async def respond(request):
        sent.append(request)
        return httpx.Response(
            200, json=await fixture.get_json(str(request.url), dict(request.url.params))
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            watcher.transport.client = client
            assert (await watcher.check("test", immediate=True)).execution_status == "COMPLETED"
            sent.clear()
            before = dict(watcher.store.target("test"))
            tables = ("items", "events", "outbox")
            before_rows = {
                table: [
                    tuple(row) for row in watcher.store.connection.execute(f"SELECT * FROM {table}")
                ]
                for table in tables
            }
            for restriction in ("paused", "backoff", "lease"):
                if restriction == "paused":
                    watcher.store.connection.execute(
                        "UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'"
                    )
                elif restriction == "backoff":
                    watcher.store.connection.execute(
                        "UPDATE platform SET paused_reason=NULL,blocked_until=? WHERE id='ticketplus'",
                        (watcher.clock() + 100,),
                    )
                else:
                    watcher.store.connection.execute(
                        "UPDATE platform SET blocked_until=0,lease_owner='other',lease_until=? WHERE id='ticketplus'",
                        (watcher.clock() + 100,),
                    )
                assert (await watcher.refresh_metadata("test")).execution_status == "DEFERRED"
                assert not sent
            watcher.store.release("other")
            result = await watcher.refresh_metadata("test")
            assert result.execution_status == "COMPLETED"
            assert result.data["request_count"] == 3
            assert all(r.url.path.endswith("getS3") for r in sent)
            assert watcher.store.target("test") == before
            assert before_rows["items"]
            for table in tables:
                assert [
                    tuple(row) for row in watcher.store.connection.execute(f"SELECT * FROM {table}")
                ] == before_rows[table]
            assert watcher.query_log.recent()["entries"][0]["mode"] == "metadata"
            assert watcher.store.platform()["lease_owner"] is None

    asyncio.run(scenario())


def test_manual_refresh_403_stops_platform_without_touching_ticket_state(harness):
    watcher = harness["watcher"]
    watcher.adapter = TicketPlusAdapter(watcher.transport)
    harness["responses"].append(httpx.Response(403, text="Denied"))
    watcher.store.sync_target(watcher.config.targets[0])
    before = copy.deepcopy(watcher.store.target("test"))
    result = asyncio.run(watcher.refresh_metadata("test"))
    assert result.data["error"]["code"] == "BLOCKED"
    assert watcher.store.platform()["paused_reason"] == "BLOCKED"
    assert watcher.store.target("test") == before
    assert len(harness["requests"]) == 1
    assert watcher.store.platform()["lease_owner"] is None


@pytest.mark.parametrize("value", [-1, True, 1.5, "7"])
def test_invalid_cache_setting_rejected(value, tmp_path):
    with pytest.raises(ValueError):
        parse_config({"http": {"metadata_cache_seconds": value}}, tmp_path)


def test_ui_setting_roundtrip_and_refresh_guard(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            state = await snapshot(client)
            assert state["settings"]["http"]["metadata_cache_seconds"] == 604800
            for seconds in (0, 604800):
                response = await mutate(
                    client, state, "/api/settings", {"http": {"metadata_cache_seconds": seconds}}
                )
                assert response.status == 200
                state = await response.json()
                assert state["settings"]["http"]["metadata_cache_seconds"] == seconds
                assert c.watcher.config.metadata_cache_seconds == seconds
            response = await mutate(
                client, state, "/api/targets", {"name": "測試", "url": ORDER, "enabled": False}
            )
            assert response.status == 200
            state = await response.json()
            ident = state["targets"][0]["id"]
            c.watcher.store.connection.execute(
                "UPDATE platform SET paused_reason='BLOCKED' WHERE id='ticketplus'"
            )
            response = await client.post(
                "/api/actions/refresh-metadata",
                headers={"X-CSRF-Token": state["csrf"]},
                json={"revision": state["revision"], "target_id": ident},
            )
            assert (await response.json())["reason"] == "PLATFORM_PAUSED"
            assert not requests
            assert (await client.post("/api/actions/refresh-metadata", json={})).status == 403
            state = await (
                await client.post(
                    "/api/actions/pause-service",
                    headers={"X-CSRF-Token": state["csrf"]},
                    json={"revision": state["revision"]},
                )
            ).json()
            response = await client.post(
                "/api/actions/refresh-metadata",
                headers={"X-CSRF-Token": state["csrf"]},
                json={"revision": state["revision"], "target_id": ident},
            )
            assert response.status == 409 and not requests

    asyncio.run(scenario())


def test_new_ticket_type_is_only_included_after_admin_refresh(harness):
    adapter, transport, target = cached_adapter(harness, metadata_cache_seconds=0)
    initial = asyncio.run(adapter.fetch(target))
    row = copy.deepcopy(transport.case["products"][0])
    row["productId"] = "p999999999"
    row["name"] = "新增區域"
    transport.case["products"].append(row)
    live = copy.deepcopy(transport.case["live"]["s000002256"]["product"][0])
    live["id"] = "p999999999"
    transport.case["live"]["s000002256"]["product"].append(live)
    harness["clock"].advance(10 * 86400)
    unchanged = asyncio.run(adapter.fetch(target))
    assert unchanged.request_count == 1 and len(unchanged.items) == len(initial.items)
    asyncio.run(adapter.refresh_metadata(target))
    updated = asyncio.run(adapter.fetch(target))
    assert updated.request_count == 1 and len(updated.items) == len(initial.items) + 1
    assert any(item.name == "新增區域" for item in updated.items)
