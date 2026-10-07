import asyncio
import os
import subprocess
import sys

import httpx
import pytest

from ticket_watcher.config import Config, Target, load_config, parse_config
from ticket_watcher.service import Watcher

PROXY = "http://warp.internal:40000"
API = "https://apis.ticketplus.com.tw/config/api/v1/get"
DISCORD = "https://discord.com/api/webhooks/123456/token"


class RecordingTransport(httpx.MockTransport):
    def __init__(self, *, error=None):
        self.requests = []
        self.closed = False
        self.error = error
        super().__init__(self.handle)

    def handle(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error("proxy unavailable", request=request)
        return httpx.Response(200, json={"id": "123456"})

    async def aclose(self):
        self.closed = True


def mock_network(monkeypatch, *, error=None):
    """Keep HTTPX's real mount routing, replace only the actual network transports."""
    direct = RecordingTransport()
    proxy = RecordingTransport(error=error)
    options = {"client": [], "proxy": []}
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        options["client"].append(kwargs)
        return real_client(transport=direct, **kwargs)

    def proxy_factory(**kwargs):
        options["proxy"].append(kwargs)
        return proxy

    monkeypatch.setattr("ticket_watcher.service.httpx.AsyncClient", client_factory)
    monkeypatch.setattr("ticket_watcher.service.httpx.AsyncHTTPTransport", proxy_factory)
    return direct, proxy, options


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_proxy_preserves_direct_local_queries(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TICKET_WATCHER_TICKETPLUS_PROXY", raising=False)
    else:
        monkeypatch.setenv("TICKET_WATCHER_TICKETPLUS_PROXY", value)
    # Generic proxy environment variables must not change application routing.
    monkeypatch.setenv("HTTPS_PROXY", "http://unexpected.internal:9999")
    monkeypatch.setenv("ALL_PROXY", "http://unexpected.internal:9999")
    direct, proxy, options = mock_network(monkeypatch)
    config = parse_config({}, tmp_path)
    assert config.ticketplus_proxy == ""

    async def run():
        async with Watcher(config) as watcher:
            await watcher.client.get(API)
            await watcher.notifier.client.post(DISCORD)

    asyncio.run(run())
    assert [str(request.url) for request in direct.requests] == [API, DISCORD]
    assert not proxy.requests and not options["proxy"]
    assert options["client"][0]["trust_env"] is False
    assert direct.closed


def test_only_exact_https_ticketplus_api_domain_uses_proxy_and_closes_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_WATCHER_TICKETPLUS_PROXY", PROXY)
    monkeypatch.setenv("HTTPS_PROXY", "http://unexpected.internal:9999")
    monkeypatch.setenv("NO_PROXY", "apis.ticketplus.com.tw")
    direct, proxy, options = mock_network(monkeypatch)
    config = load_config()
    config = Config(database_path=tmp_path / "state.db", ticketplus_proxy=config.ticketplus_proxy)
    direct_urls = [
        DISCORD,
        "https://ticketplus.com.tw/activity/e000000001",
        "http://apis.ticketplus.com.tw/config/api/v1/get",
        "https://apis.ticketplus.com.tw.evil.test/config/api/v1/get",
        "https://sub.apis.ticketplus.com.tw/config/api/v1/get",
        "https://example.com/",
    ]

    async def run():
        async with Watcher(config) as watcher:
            await watcher.client.get(API)
            await watcher.client.get("https://apis.ticketplus.com.tw/config/api/v1/getS3")
            for url in direct_urls:
                await watcher.client.get(url)
            assert watcher.client is watcher.transport.client is watcher.notifier.client

    asyncio.run(run())
    assert len(proxy.requests) == 2
    assert [str(request.url) for request in direct.requests] == direct_urls
    assert options["proxy"] == [{"proxy": PROXY, "trust_env": False}]
    assert options["client"][0]["trust_env"] is False
    assert options["client"][0]["follow_redirects"] is False
    assert options["client"][0]["timeout"] == config.timeout
    assert direct.closed and proxy.closed


@pytest.mark.parametrize("error", [httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout])
def test_proxy_failure_backs_off_without_direct_fallback_and_discord_still_delivers(
    tmp_path, monkeypatch, error
):
    direct, proxy, _ = mock_network(monkeypatch, error=error)
    monkeypatch.setenv("TEST_WARP_DISCORD_WEBHOOK", DISCORD)
    config = Config(
        database_path=tmp_path / "state.db",
        ticketplus_proxy=PROXY,
        webhook_url_env="TEST_WARP_DISCORD_WEBHOOK",
        targets=(Target("test", "test", "https://ticketplus.com.tw/activity/e000000001"),),
    )
    now = 1_800_000_000.0

    async def run():
        async with Watcher(config, clock=lambda: now) as watcher:
            result = await watcher.check("test", immediate=True)
            assert result.execution_status == "FAILED"
            assert result.data["error"]["code"] == "NETWORK"
            assert result.data["request_count"] == 1
            state = watcher.store.target("test")
            assert state["last_error"] == "NETWORK"
            assert state["next_check"] >= now + config.backoff[0]
            assert (await watcher.check("test", immediate=True)).execution_status == "DEFERRED"

    asyncio.run(run())
    assert len(proxy.requests) == 1
    assert len(direct.requests) == 1
    assert direct.requests[0].method == "POST"
    assert str(direct.requests[0].url) == DISCORD + "?wait=true"


def test_injected_client_remains_authoritative_and_is_not_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_WATCHER_TICKETPLUS_PROXY", PROXY)
    transport = RecordingTransport()
    client = httpx.AsyncClient(transport=transport)

    def unexpected_proxy(**kwargs):
        pytest.fail("An injected client must not create an additional proxy transport")

    monkeypatch.setattr("ticket_watcher.service.httpx.AsyncHTTPTransport", unexpected_proxy)

    async def run():
        async with Watcher(Config(database_path=tmp_path / "state.db"), client=client) as watcher:
            await watcher.client.get(API)
        assert not client.is_closed and not transport.closed
        await client.aclose()

    asyncio.run(run())
    assert len(transport.requests) == 1 and transport.closed


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:40000",
        "https://proxy.internal:443/",
        "http://[::1]:40000",
        "http://user:secret@proxy.internal:40000",
    ],
)
def test_valid_http_proxy_endpoints_are_loaded_without_showing_credentials(monkeypatch, value):
    monkeypatch.setenv("TICKET_WATCHER_TICKETPLUS_PROXY", value)
    config = Config()
    assert config.ticketplus_proxy == value
    assert value not in repr(config)


@pytest.mark.parametrize(
    "value",
    [
        " ",
        "127.0.0.1:40000",
        "socks5://user:secret@127.0.0.1:40000",
        "http://",
        "http://proxy.internal:",
        "http://proxy.internal:0",
        "http://proxy.internal:65536",
        "http://user:secret@proxy.internal:bad",
        "http://user:secret@proxy.internal/path",
        "http://proxy.internal?token=secret",
        "http://proxy.internal#secret",
        "http://[invalid]:40000",
        "http://proxy.internal\n",
        "http://proxy.internal\\secret",
    ],
)
def test_invalid_proxy_configuration_is_rejected_with_a_redacted_error(monkeypatch, value):
    monkeypatch.setenv("TICKET_WATCHER_TICKETPLUS_PROXY", value)
    with pytest.raises(ValueError, match="TICKET_WATCHER_TICKETPLUS_PROXY") as error:
        load_config()
    assert "secret" not in str(error.value)
    assert "proxy.internal" not in str(error.value)


@pytest.mark.parametrize("value", [None, False, 40000])
def test_invalid_programmatic_proxy_configuration_is_rejected(value):
    with pytest.raises(ValueError, match="TICKET_WATCHER_TICKETPLUS_PROXY"):
        Config(ticketplus_proxy=value)


def test_health_remains_lightweight_when_production_proxy_is_configured(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("app:\n  database_path: missing.db\ntargets: []\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from ticket_watcher.cli import main; "
            "main(['health', '--config', sys.argv[1]]); "
            "print([m for m in ('httpx','aiohttp','ticket_watcher.service') if m in sys.modules])",
            str(path),
        ],
        env={**os.environ, "TICKET_WATCHER_TICKETPLUS_PROXY": PROXY},
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == "[]"
