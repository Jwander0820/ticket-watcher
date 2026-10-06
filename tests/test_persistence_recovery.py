import asyncio
import copy

import httpx
import pytest
from conftest import URL, Clock, Source

import ticket_watcher.web as web_module
from ticket_watcher.private_io import atomic_text, read_webhooks
from ticket_watcher.service import Watcher
from ticket_watcher.storage import Store
from ticket_watcher.web import Controller

HOOK = "https://discord.com/api/webhooks/123/recovery_test_only"


def config_file(tmp_path):
    path = tmp_path / "ui-config.yaml"
    atomic_text(
        path,
        "app:\n  database_path: state.db\nnotifications:\n"
        "  webhook_url_env: PERSISTENCE_TEST_UNUSED\n"
        "  worker_alerts_enabled: false\ntargets: []\n",
    )
    return path


@pytest.mark.parametrize("raw_credentials", [None, '{\r\n  "default": "' + HOOK + '"\r\n}\r\n'])
def test_failed_save_restores_exact_files_and_allows_retry(tmp_path, monkeypatch, raw_credentials):
    path = config_file(tmp_path)
    secret = tmp_path / "discord-webhooks.json"
    if raw_credentials is not None:
        atomic_text(secret, raw_credentials)
    original = web_module.atomic_text

    def fail_config(destination, text):
        if destination == path:
            raise OSError("simulated config write failure")
        original(destination, text)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            c = Controller(path, watcher_factory=lambda cfg: Watcher(cfg, client=client))
            await c.start()
            revision, old_document = c.revision, path.read_bytes()
            document = copy.deepcopy(c.document)
            document["polling"] = {"normal_interval_seconds": [400, 900]}
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(web_module, "atomic_text", fail_config)
                    with pytest.raises(OSError):
                        await c.save(document, {"default": HOOK})
                assert c.task is not None and not c.task.done()
                assert c.watcher is not None
                assert path.read_bytes() == old_document
                assert secret.exists() == (raw_credentials is not None)
                if raw_credentials is not None:
                    assert secret.read_bytes() == raw_credentials.encode()
                c.require_revision(revision)
                await c.save(document, read_webhooks(secret))
                assert c.config.normal_interval == (400, 900)
            finally:
                await c.close()

    asyncio.run(scenario())


def test_failed_initial_write_keeps_monitor_running(tmp_path, monkeypatch):
    path = config_file(tmp_path)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        ) as client:
            c = Controller(path, watcher_factory=lambda cfg: Watcher(cfg, client=client))
            await c.start()
            try:

                def fail(*args):
                    raise OSError("simulated disk failure")

                with monkeypatch.context() as patch:
                    patch.setattr(web_module, "atomic_text", fail)
                    with pytest.raises(OSError):
                        await c.save(c.document, {})
                assert c.task is not None and not c.task.done()
                assert c.watcher is not None
                c.require_revision(c.revision)
            finally:
                await c.close()

    asyncio.run(scenario())


def test_failed_rollback_retries_before_starting_notifications(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    secret = tmp_path / "discord-webhooks.json"
    raw = '{\n  "default": "' + HOOK + '"\n}\n'
    atomic_text(secret, raw)
    monkeypatch.setattr(web_module, "WORKER_RETRY_SECONDS", (0.01,))
    monkeypatch.setattr(web_module, "WORKER_STABLE_SECONDS", 0.01)
    original = web_module.atomic_text
    calls = 0

    def fail_after_first_write(destination, text):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("simulated config and rollback failure")
        original(destination, text)

    async def scenario():
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "123"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            c = Controller(path, watcher_factory=lambda cfg: Watcher(cfg, client=client))
            await c.start()
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(web_module, "atomic_text", fail_after_first_write)
                    with pytest.raises(OSError):
                        await c.save(c.document, {"default": HOOK + "_changed"})
                    assert c.task is not None and not c.task.done()
                    assert c.watcher is None
                    await asyncio.sleep(0.025)
                    assert c.watcher is None and not requests
                async with asyncio.timeout(2):
                    while c.watcher is None or c.runner_error:
                        await asyncio.sleep(0.005)
                assert secret.read_bytes() == raw.encode()
                c.require_revision(c.revision)
                assert not requests
            finally:
                await c.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement_fails", [False, True])
def test_channel_reload_cancels_old_notice_during_discord_cooldown(tmp_path, replacement_fails):
    path = config_file(tmp_path)
    clock, requests = Clock(), []
    source = Source(clock)
    calls = 0

    async def scenario():
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "123"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

            def factory(config):
                nonlocal calls
                calls += 1
                if replacement_fails and calls == 3:
                    raise OSError("simulated constructor failure")
                return Watcher(config, client=client, clock=clock, adapter=source)

            c = Controller(path, watcher_factory=factory)
            await c.start()
            try:
                document = copy.deepcopy(c.document)
                document["channels"] = [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
                document["targets"] = [{"id": "test", "url": URL, "channel_id": "a"}]
                credentials = {"a": HOOK, "b": HOOK + "_b"}
                await c.save(document, credentials)
                c.watcher.store.connection.execute(
                    "UPDATE platform SET blocked_until=? WHERE id='discord'", (clock() + 120,)
                )
                for status in ("SOLD_OUT", "AVAILABLE"):
                    source.push(status)
                    result = await c.watcher._check("test", immediate=True)
                event_id = result.data["event_id"]
                before = c.watcher.store.items("test")
                document = copy.deepcopy(c.document)
                document["targets"][0]["channel_id"] = "b"
                await c.save(document, credentials)
                store = Store(c.config.database_path)
                try:
                    assert store.notification_status(event_id)["status"] == "CANCELLED"
                    assert store.items("test") == before
                finally:
                    store.close()
                document = copy.deepcopy(c.document)
                document["targets"][0]["channel_id"] = "a"
                await c.save(document, credentials)
                clock.advance(121)
                await c.watcher.notifier.deliver()
                assert c.watcher.store.notification_status(event_id)["status"] == "CANCELLED"
                assert not requests
            finally:
                await c.close()

    asyncio.run(scenario())
