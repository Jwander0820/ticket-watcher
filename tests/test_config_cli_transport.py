import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from ticket_watcher.cli import main
from ticket_watcher.config import load_config, parse_config, validate_url
from ticket_watcher.models import SourceError
from ticket_watcher.transport import MAX_RETRY_AFTER_SECONDS, PublicTransport, retry_after


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_stop_time_is_normalized_to_none(tmp_path, value):
    config = parse_config(
        {
            "targets": [
                {
                    "id": "test",
                    "url": "https://ticketplus.com.tw/activity/e000000001",
                    "stop_at": value,
                }
            ]
        },
        tmp_path,
    )
    assert config.targets[0].stop_at is None


@pytest.mark.parametrize("value", [False, True, 0, 1, [], {}, ["2026-10-10"]])
def test_non_date_stop_time_is_rejected_before_monitor_starts(tmp_path, value):
    with pytest.raises(ValueError):
        parse_config(
            {
                "targets": [
                    {
                        "id": "test",
                        "url": "https://ticketplus.com.tw/activity/e000000001",
                        "stop_at": value,
                    }
                ]
            },
            tmp_path,
        )


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
    assert config.request_gap == (5, 15)
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
        "polling:\n  normal_interval_seconds: [300, 604801]",
        "polling:\n  active_window_seconds: 10000000000000",
        "polling:\n  active_stable_checks: 1001",
        "polling:\n  quiet_windows: [{start: '00:00', end: '07:00', interval_seconds: [300, 604801]}]",
        "http:\n  min_request_gap_seconds: [5, 601]",
        "http:\n  min_request_gap_seconds: 601",
        "http:\n  timeout_seconds: 301",
        "http:\n  metadata_cache_seconds: 31536001",
        "http:\n  backoff_seconds: [900, 604801]",
        "notifications:\n  delivery_ttl_seconds: 604801",
        "app:\n  retention_days: 3651",
    ],
)
def test_invalid_config_fails_clearly(tmp_path, data):
    path = tmp_path / "bad.yaml"
    path.write_text(data)
    with pytest.raises(ValueError):
        load_config(path)


def test_largest_allowed_settings_are_accepted(tmp_path):
    config = parse_config(
        {
            "polling": {
                "normal_interval_seconds": [604800, 604800],
                "active_interval_seconds": [604800, 604800],
                "active_window_seconds": 604800,
                "exit_active_after_no_available_checks": 1000,
                "active_stable_checks": 1000,
                "quiet_windows": [
                    {"start": "00:00", "end": "07:00", "interval_seconds": [604800, 604800]}
                ],
            },
            "http": {
                "min_request_gap_seconds": [600, 600],
                "timeout_seconds": 300,
                "metadata_cache_seconds": 31536000,
            },
            "notifications": {"delivery_ttl_seconds": 604800},
            "app": {"retention_days": 3650},
        },
        tmp_path,
    )
    assert config.normal_interval == (604800, 604800)
    assert config.timeout == 300 and config.request_gap == (600, 600)


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


def test_retry_after_caps_server_wait():
    now = 1_800_000_000
    assert retry_after("99999999999999", now) == MAX_RETRY_AFTER_SECONDS
    assert retry_after("Fri, 31 Dec 9999 23:59:59 GMT", now) == MAX_RETRY_AFTER_SECONDS


def test_long_rate_limit_keeps_status_readable(harness):
    h = harness
    wait = retry_after("99999999999999", h["clock"]())
    h["source"].values.append(SourceError("RATE_LIMITED", "限制", wait))
    result = asyncio.run(h["watcher"].check("test"))
    assert result.execution_status == "FAILED"
    platform = h["watcher"].store.platform()
    assert platform["blocked_until"] == h["clock"]() + MAX_RETRY_AFTER_SECONDS
    status = h["watcher"].status().data
    assert status["platform"]["blocked_until"] and status["targets"][0]["next_allowed_at"]


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
        client, h["watcher"].store, replace(h["config"], request_gap=(5, 5)), h["clock"]
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
