import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from ticket_watcher.cli import main
from ticket_watcher.config import load_config, validate_url
from ticket_watcher.models import SourceError
from ticket_watcher.transport import PublicTransport, retry_after


@pytest.mark.parametrize(
    "url",
    [
        "http://ticketplus.com.tw/activity/e000000001",
        "https://ticketplus.com.tw.evil.test/activity/e000000001",
        "https://user:secret@ticketplus.com.tw/activity/e000000001",
        "https://ticketplus.com.tw/order/e000000001",
        "https://ticketplus.com.tw/activity/e000000001?token=secret",
    ],
)
def test_public_url_validation_rejects_unsafe_or_nonactivity_routes(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_example_config_is_valid_and_disabled_by_default():
    config = load_config("config.example.yaml")
    assert config.request_gap == 5
    assert config.normal_interval == (300, 900)
    assert config.targets[0].enabled is False


@pytest.mark.parametrize(
    "data",
    [
        "http:\n  platform_concurrency: 2",
        "polling:\n  normal_interval_seconds: [1, 2]",
        "http:\n  min_request_gap_seconds: 0",
        "app:\n  unknown_setting: true",
        "notifications:\n  delivery_ttl_seconds: false",
    ],
)
def test_invalid_config_fails_clearly(tmp_path, data):
    path = tmp_path / "bad.yaml"
    path.write_text(data)
    with pytest.raises(ValueError):
        load_config(path)


def test_capabilities_does_not_create_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["platforms"][0]["granularity"] == "SESSION"
    assert not (tmp_path / "data").exists()


def test_cli_option_before_and_after_operation_and_json_failure(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("targets: []")
    assert main(["--config", str(path), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result_source"] == "CACHE"
    assert main(["status", "--config", str(path), "--json", "--limit", "0"]) == 2
    assert json.loads(capsys.readouterr().out)["execution_status"] == "FAILED"


def test_retry_after_supports_seconds_and_http_date():
    now = 1_800_000_000
    assert retry_after("120", now) == 120
    value = format_datetime(datetime.fromtimestamp(now + 300, UTC), usegmt=True)
    assert retry_after(value, now) == 300
    assert retry_after("invalid", now) == 0
    assert retry_after("-1", now) == 0
    assert retry_after("inf", now) == 0
    assert retry_after("nan", now) == 0


@pytest.mark.parametrize(
    "status,body,headers,code",
    [
        (403, {}, {}, "BLOCKED"),
        (429, {}, {"Retry-After": "4000"}, "RATE_LIMITED"),
        (503, {}, {}, "NETWORK"),
        (200, "<html>cf-chl-challenge</html>", {"content-type": "text/html"}, "BLOCKED"),
        (200, "garbage", {}, "PARSE"),
    ],
)
def test_transport_classifies_http_errors_without_retries(harness, status, body, headers, code):
    h = harness
    response = (
        httpx.Response(status, json=body, headers=headers)
        if isinstance(body, dict)
        else httpx.Response(status, text=body, headers=headers)
    )
    calls = []

    def handle(request):
        calls.append(request)
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = PublicTransport(
        client, h["watcher"].store, replace(h["config"], request_gap=5), h["clock"]
    )
    transport.owner = "test-owner"
    h["watcher"].store.acquire(transport.owner, h["clock"](), 180)
    with pytest.raises(SourceError) as error:
        asyncio.run(transport.get_json("https://apis.ticketplus.com.tw/config/api/v1/get", {}))
    assert error.value.code == code and len(calls) == 1
    if status == 429:
        assert error.value.retry_after == 4000


def test_stop_at_requires_timezone(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "targets:\n- id: event\n  url: https://ticketplus.com.tw/activity/e000000001\n  stop_at: '2026-11-01T00:00:00'",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="時區"):
        load_config(path)
