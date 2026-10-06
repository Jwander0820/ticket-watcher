import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import URL

from ticket_watcher.config import Channel, load_config
from ticket_watcher.models import Observation, TicketItem, TicketStatus
from ticket_watcher.private_io import atomic_text, read_webhooks
from ticket_watcher.service import Watcher
from ticket_watcher.web import CONTROLLER, create_app

HOOK = "https://discord.com/api/webhooks/123/test_private_value"
HOOK_TWO = "https://discord.com/api/webhooks/456/second_private_value"


@asynccontextmanager
async def panel(tmp_path, *, monitor=False):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "123456"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        app = create_app(
            tmp_path / "ui-config.yaml",
            monitor=monitor,
            watcher_factory=lambda c: Watcher(c, client=transport),
        )
        async with TestClient(TestServer(app)) as client:
            yield client, app[CONTROLLER], requests


async def snapshot(client):
    response = await client.get("/api/state")
    assert response.status == 200
    return await response.json()


async def mutate(client, state, path, value=None, method="POST"):
    return await client.request(
        method,
        path,
        headers={"X-CSRF-Token": state["csrf"]},
        json={"revision": state["revision"], "value": value},
    )


def test_ui_starts_empty_without_external_requests(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    async def scenario():
        async with panel(tmp_path, monitor=True) as (client, c, requests):
            state = await snapshot(client)
            assert state["targets"] == [] and state["runner_active"]
            assert not state["channels"][0]["configured"]
            assert requests == []
            home = await client.get("/")
            assert home.status == 200 and "新增監控" in await home.text()
            assert "frame-ancestors 'none'" in home.headers["Content-Security-Policy"]
            assert (await client.get("/static/app.js")).status == 200
            assert (await client.get("/static/discord-webhooks.json")).status == 404
        assert c.task is None and c.watcher is None

    asyncio.run(scenario())


def test_ui_rejects_cross_site_and_missing_csrf(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            state = await snapshot(client)
            assert (
                await client.get("/api/state", headers={"Host": "attacker.invalid"})
            ).status == 403
            assert (
                await client.get("/api/state", headers={"Origin": "https://attacker.invalid"})
            ).status == 403
            assert (
                await client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"})
            ).status == 403
            assert (await client.post("/api/settings", json={})).status == 403
            assert (
                await client.post(
                    "/api/settings", headers={"X-CSRF-Token": state["csrf"]}, data="{}"
                )
            ).status == 415
            assert not requests and c.config.targets == ()

    asyncio.run(scenario())


def test_channels_are_private_and_test_delivery_routes_to_selected_channel(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            state = await snapshot(client)
            response = await mutate(
                client, state, "/api/channels", {"name": "演唱會", "webhook_url": HOOK}
            )
            assert response.status == 200
            state = await response.json()
            ident = state["channels"][-1]["id"]
            assert state["channels"][-1]["configured"]
            assert HOOK not in json.dumps(state) and HOOK not in c.path.read_text(encoding="utf-8")
            assert read_webhooks(c.config.secrets_path)[ident] == HOOK
            assert not requests  # Saving credentials never sends a test message.
            response = await mutate(
                client,
                state,
                f"/api/channels/{ident}",
                {"name": "演出通知", "webhook_url": ""},
                "PUT",
            )
            state = await response.json()
            assert read_webhooks(c.config.secrets_path)[ident] == HOOK
            response = await client.post(
                "/api/actions/test-channel",
                headers={"X-CSRF-Token": state["csrf"]},
                json={"revision": state["revision"], "channel_id": ident},
            )
            result = await response.json()
            assert result["notification"]["status"] == "SENT"
            assert len(requests) == 1 and str(requests[0].url).split("?")[0] == HOOK
            assert HOOK not in json.dumps(c.watcher.events(detail=True).to_dict())
            assert json.loads(requests[0].content)["allowed_mentions"] == {"parse": []}

    asyncio.run(scenario())


def test_target_crud_validation_channel_use_and_reload(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            state = await snapshot(client)
            response = await mutate(
                client, state, "/api/channels", {"name": "票務", "webhook_url": HOOK}
            )
            state = await response.json()
            channel = state["channels"][-1]["id"]
            value = {"name": "台北場", "url": URL, "channel_id": channel, "enabled": False}
            response = await mutate(client, state, "/api/targets", value)
            assert response.status == 200
            state = await response.json()
            ident = state["targets"][0]["id"]
            assert load_config(c.path).targets[0].channel_id == channel
            assert (
                await mutate(client, state, f"/api/channels/{channel}", method="DELETE")
            ).status == 409
            before = c.path.read_bytes()
            assert (
                await mutate(
                    client, state, f"/api/targets/{ident}", {**value, "channel_id": "absent"}, "PUT"
                )
            ).status == 400
            assert (
                await mutate(client, state, "/api/targets", {**value, "url": "https://example.com"})
            ).status == 400
            assert c.path.read_bytes() == before
            response = await mutate(
                client,
                state,
                f"/api/targets/{ident}",
                {**value, "name": "台北場更新", "enabled": True},
                "PUT",
            )
            state = await response.json()
            assert c.watcher.config.targets[0].enabled
            assert state["targets"][0]["name"] == "台北場更新"
            response = await mutate(client, state, f"/api/targets/{ident}", method="DELETE")
            state = await response.json()
            response = await mutate(client, state, f"/api/channels/{channel}", method="DELETE")
            assert response.status == 200
            assert read_webhooks(c.config.secrets_path) == {}
            assert not requests

    asyncio.run(scenario())


def test_settings_reload_preserves_baseline_schedule_and_platform_backoff(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            state = await snapshot(client)
            state = await (
                await mutate(client, state, "/api/targets", {"name": "測試", "url": URL})
            ).json()
            target = c.config.targets[0]
            c.watcher.store.sync_target(target)
            observation = Observation(
                "測試",
                URL,
                c.watcher.clock(),
                (TicketItem("s000000001", "s000000001", "場次", TicketStatus.SOLD_OUT, URL),),
            )
            c.watcher._apply(target, observation)
            items = c.watcher.store.items(target.id)
            due = c.watcher.store.target(target.id)["next_check"]
            c.watcher.store.connection.execute(
                "UPDATE platform SET blocked_until=9999999999 WHERE id='ticketplus'"
            )
            response = await mutate(
                client,
                state,
                "/api/settings",
                {
                    "polling": {"normal_interval_seconds": [600, 1200]},
                    "http": {"timeout_seconds": 30},
                },
            )
            assert response.status == 200
            assert c.watcher.config.normal_interval == (600, 1200)
            assert c.watcher.transport.config.timeout == 30
            assert c.watcher.notifier.config.timeout == 30
            assert c.watcher.store.target(target.id)["next_check"] == due
            assert c.watcher.store.items(target.id) == items
            assert c.watcher.store.platform()["blocked_until"] == 9999999999
            state = await response.json()
            assert (
                await mutate(
                    client, state, "/api/settings", {"http": {"min_request_gap_seconds": 1}}
                )
            ).status == 400
            assert not requests

    asyncio.run(scenario())


def test_stale_ui_revision_and_external_edits_are_not_overwritten(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            old = await snapshot(client)
            new = await (
                await mutate(client, old, "/api/targets", {"name": "一", "url": URL})
            ).json()
            assert (
                await mutate(client, old, "/api/targets", {"name": "二", "url": URL})
            ).status == 409
            c.path.write_text(
                c.path.read_text(encoding="utf-8") + "\n# outside edit\n", encoding="utf-8"
            )
            assert (await snapshot(client))["external_changes"]
            assert (
                await mutate(client, new, "/api/targets", {"name": "三", "url": URL})
            ).status == 409
            assert "outside edit" in c.path.read_text(encoding="utf-8")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/secret",
        "https://example.com/secret",
        "https://discord.com/api/webhooks/123/token?leak=secret",
    ],
)
def test_invalid_webhook_is_rejected_without_echoing_value(tmp_path, url):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            response = await mutate(
                client,
                await snapshot(client),
                "/api/channels",
                {"name": "測試", "webhook_url": url},
            )
            assert response.status == 400 and "secret" not in await response.text()
            assert not c.config.secrets_path.exists() and not requests

    asyncio.run(scenario())


def test_release_routes_each_target_to_its_channel_and_skips_unconfigured(harness):
    h = harness
    config = replace(
        h["config"],
        channels=(Channel("a", "A"), Channel("b", "B")),
        targets=(
            replace(h["config"].targets[0], id="a", channel_id="a"),
            replace(h["config"].targets[0], id="b", channel_id="b"),
            replace(h["config"].targets[0], id="missing"),
        ),
    )
    atomic_text(config.secrets_path, json.dumps({"a": HOOK, "b": HOOK_TWO}))
    w = Watcher(config, client=h["client"], clock=h["clock"], adapter=h["source"])

    async def scenario():
        for target in config.targets:
            w.store.sync_target(target)
            for status in ("SOLD_OUT", "AVAILABLE"):
                h["source"].push(status)
                w._apply(target, await h["source"].fetch(target))
        result = await w.notifier.deliver()
        assert result["sent"] == 2
        assert {str(r.url).split("?")[0] for r in h["requests"]} == {HOOK, HOOK_TWO}
        missing = w.events("missing").data["events"][0]
        assert missing["notification_status"] == "PENDING" and missing["attempts"] == 0

    try:
        asyncio.run(scenario())
    finally:
        w.store.close()


def test_changing_channel_cancels_old_notice_without_resetting_ticket_baseline(harness):
    h = harness
    target = replace(h["config"].targets[0], channel_id="a")
    config = replace(
        h["config"], targets=(target,), channels=(Channel("a", "A"), Channel("b", "B"))
    )
    atomic_text(config.secrets_path, json.dumps({"a": HOOK, "b": HOOK_TWO}))
    w = Watcher(config, client=h["client"], clock=h["clock"], adapter=h["source"])

    async def scenario():
        w.store.sync_target(target)
        for status in ("SOLD_OUT", "AVAILABLE"):
            h["source"].push(status)
            w._apply(target, await h["source"].fetch(target))
        before = w.store.items(target.id)
        updated = replace(target, channel_id="b")
        w.store.sync_target(updated)
        w.notifier.config = replace(config, targets=(updated,))
        await w.notifier.deliver()
        assert w.events().data["events"][0]["notification_status"] == "CANCELLED"
        assert w.store.items(target.id) == before and not h["requests"]

    try:
        asyncio.run(scenario())
    finally:
        w.store.close()
