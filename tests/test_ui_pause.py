import asyncio
import copy

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import URL, Source
from test_web import mutate, panel, snapshot
from test_worker_recovery import eventually

import ticket_watcher.web as web_module
from ticket_watcher.config import load_config, parse_config
from ticket_watcher.health import read_health
from ticket_watcher.private_io import atomic_text
from ticket_watcher.service import Watcher
from ticket_watcher.web import CONTROLLER, create_app


async def control(client, state, action, **fields):
    return await client.post(
        f"/api/actions/{action}",
        headers={"X-CSRF-Token": state["csrf"]},
        json={"revision": state["revision"], **fields},
    )


def test_pause_persists_across_settings_and_restart_and_preserves_baseline(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            response = await mutate(
                client,
                await snapshot(client),
                "/api/targets",
                {"name": "保留基準", "url": URL, "enabled": True, "auto_stop": False},
            )
            state = await response.json()
            ident = state["targets"][0]["id"]
            source = Source(c.watcher.clock)
            source.push("SOLD_OUT")
            c.watcher.adapter = source
            assert (await control(client, state, "check", target_id=ident)).status == 200
            items = c.watcher.store.items(ident)
            schedule = c.watcher.store.target(ident)
            paused = await control(client, state, "pause-service")
            assert paused.status == 200
            state = await paused.json()
            assert state["ui_paused"] and not state["runner_active"]
            assert load_config(c.path).ui_paused
            assert c.watcher.store.items(ident) == items
            assert c.watcher.store.target(ident) == schedule
            for action in ("check", "test-channel", "resume"):
                assert (await control(client, state, action, target_id=ident)).status == 409
            response = await mutate(client, state, "/api/settings", state["settings"])
            assert response.status == 200
            assert (await response.json())["ui_paused"]
            assert c.config.targets[0].enabled
            assert c.watcher.store.items(ident) == items
            assert not requests

        async with panel(tmp_path, monitor=True) as (client, c, requests):
            await eventually(lambda: c.watcher.health().data["process_healthy"])
            state = await snapshot(client)
            assert state["ui_paused"] and not state["runner_active"]
            assert read_health(c.config).data["process_healthy"]
            assert not requests
            response = await control(client, state, "resume-service")
            assert response.status == 200
            updated = await response.json()
            assert not updated["ui_paused"] and updated["runner_active"]
            assert not load_config(c.path).ui_paused
            assert c.watcher.store.items(ident) == items
            assert c.watcher.store.target(ident) == schedule
            assert c.config.targets[0].enabled

    asyncio.run(scenario())


def test_pause_suspends_pending_delivery_until_explicit_resume(tmp_path):
    async def scenario():
        async with panel(tmp_path, monitor=True) as (client, c, requests):
            state = await (await control(client, await snapshot(client), "pause-service")).json()
            response = await mutate(
                client,
                state,
                "/api/channels/default",
                {"name": "default", "webhook_url": "https://discord.com/api/webhooks/123/mock"},
                method="PUT",
            )
            state = await response.json()
            with c.watcher.store.transaction() as db:
                c.watcher.store.enqueue(
                    db,
                    "paused-notice",
                    None,
                    "SYSTEM",
                    c.watcher.clock(),
                    {"message": "mock pending"},
                    600,
                    True,
                )
            await eventually(lambda: c.watcher.health().data["process_healthy"])
            assert c.watcher.store.notification_status("paused-notice")["status"] == "PENDING"
            assert not requests
        async with panel(tmp_path, monitor=True) as (client, c, requests):
            await eventually(lambda: c.watcher.health().data["process_healthy"])
            assert not requests
            response = await control(client, await snapshot(client), "resume-service")
            assert response.status == 200
            await eventually(lambda: bool(requests))
            assert c.watcher.store.notification_status("paused-notice")["status"] == "SENT"
            assert len(requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("manual", [False, True], ids=["scheduled", "manual"])
def test_pause_cancels_inflight_checks_and_does_not_restart_them(tmp_path, manual):
    path = tmp_path / "ui-config.yaml"
    atomic_text(path, f"targets:\n  - id: test\n    name: test\n    url: {URL}\n")

    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class BlockingSource:
            async def fetch(self, target):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as transport:
            app = create_app(
                path,
                monitor=not manual,
                watcher_factory=lambda cfg: Watcher(
                    cfg, client=transport, adapter=BlockingSource()
                ),
            )
            async with TestClient(TestServer(app)) as client:
                c = app[CONTROLLER]
                state = await snapshot(client)
                request = None
                if manual:
                    request = asyncio.create_task(control(client, state, "check", target_id="test"))
                await asyncio.wait_for(entered.wait(), 3)
                response = await control(client, state, "pause-service")
                assert response.status == 200 and cancelled.is_set()
                assert (await response.json())["ui_paused"]
                assert not c.actions
                if request:
                    assert (await request).status == 409
                assert not c.watcher.events().data["events"]

    asyncio.run(scenario())


def test_paused_recovery_never_sends_worker_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(web_module, "WORKER_RETRY_SECONDS", (0.01,))

    async def scenario():
        async with panel(tmp_path, monitor=True) as (client, c, requests):

            async def fail_standby():
                raise OSError("local storage failure")

            monkeypatch.setattr(c, "_standby", fail_standby)
            await control(client, await snapshot(client), "pause-service")
            await eventually(lambda: c.restart_count >= 1)
            assert c.config.ui_paused and not requests
            assert not c.watcher.events().data["events"]

    asyncio.run(scenario())


def test_pause_security_revision_and_failed_resume_stays_paused(tmp_path, monkeypatch):
    async def scenario():
        async with panel(tmp_path, monitor=True) as (client, c, requests):
            old = await snapshot(client)
            assert (await client.post("/api/actions/pause-service", json={})).status == 403
            state = await (await control(client, old, "pause-service")).json()
            assert (await control(client, old, "resume-service")).status == 409

            def fail_write(*args):
                raise OSError("disk full")

            monkeypatch.setattr(web_module, "atomic_text", fail_write)
            response = await control(client, state, "resume-service")
            assert response.status == 500
            assert c.config.ui_paused and load_config(c.path).ui_paused
            assert not requests

    asyncio.run(scenario())


def test_ui_paused_requires_boolean(tmp_path):
    data = {"app": {"ui_paused": "false"}}
    with pytest.raises(ValueError):
        parse_config(copy.deepcopy(data), tmp_path)


@pytest.mark.parametrize("manual", [False, True], ids=["queued-delivery", "test-channel"])
def test_pause_drains_inflight_discord_requests(tmp_path, manual):
    path = tmp_path / "ui-config.yaml"
    atomic_text(path, "app:\n  database_path: watcher.db\ntargets: []\n")
    atomic_text(
        tmp_path / "discord-webhooks.json",
        '{"default": "https://discord.com/api/webhooks/123/mock"}',
    )

    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        requests = []

        async def respond(request):
            requests.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            app = create_app(
                path,
                monitor=not manual,
                watcher_factory=lambda cfg: Watcher(cfg, client=transport),
            )
            async with TestClient(TestServer(app)) as client:
                c = app[CONTROLLER]
                state = await snapshot(client)
                request = None
                if manual:
                    request = asyncio.create_task(
                        control(client, state, "test-channel", channel_id="default")
                    )
                else:
                    with c.watcher.store.transaction() as db:
                        c.watcher.store.enqueue(
                            db,
                            "inflight",
                            None,
                            "SYSTEM",
                            c.watcher.clock(),
                            {"message": "mock"},
                            600,
                            True,
                        )
                    c.watcher.wake()
                await asyncio.wait_for(entered.wait(), 3)
                response = await control(client, state, "pause-service")
                assert response.status == 200 and cancelled.is_set()
                assert c.config.ui_paused and len(requests) == 1
                assert not c.actions
                if request:
                    assert (await request).status == 409
                assert all(
                    event["notification_status"] != "SENT"
                    for event in c.watcher.events().data["events"]
                )

    asyncio.run(scenario())
