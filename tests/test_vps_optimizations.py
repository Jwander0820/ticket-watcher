import asyncio
import gzip
import json
import sqlite3
import subprocess
import sys
import zlib
from dataclasses import replace

import httpx
import pytest
from test_monitoring import check
from test_notifications import enable
from test_web import panel, snapshot

from ticket_watcher.cli import main, parser
from ticket_watcher.config import Config
from ticket_watcher.health import read_health
from ticket_watcher.http_body import ResponseBodyError, read_body
from ticket_watcher.models import SourceError
from ticket_watcher.web import create_app


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, stall=False):
        self.chunks = chunks
        self.stall = stall
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_bounded_body_accepts_valid_stream_at_limit(encoding):
    body = b'{"value":"' + b"a" * 1000 + b'"}'
    wire = {"identity": lambda b: b, "gzip": gzip.compress, "deflate": zlib.compress}[encoding](
        body
    )

    async def scenario():
        stream = Chunks([wire[i : i + 7] for i in range(0, len(wire), 7)])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream)
            )
        ) as client:
            async with client.stream("GET", "https://example.invalid/") as response:
                assert await read_body(response, len(body)) == body
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "encoding,chunks",
    [
        ("identity", [b"a" * 512] * 100),
        ("gzip", [gzip.compress(b"a" * 1_000_000), b"never read"]),
        ("gzip", [gzip.compress(b"hello")[:-1]]),
        ("deflate", [b"invalid compression"]),
        ("br", [b"not accepted"]),
    ],
)
def test_bounded_body_stops_oversize_or_invalid_stream(encoding, chunks):
    async def scenario():
        stream = Chunks(chunks)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream)
            )
        ) as client:
            async with client.stream("GET", "https://example.invalid/") as response:
                with pytest.raises(ResponseBodyError):
                    await read_body(response, 2048)
        assert stream.closed
        assert stream.reads <= 5
        if len(chunks) > 1:
            assert stream.reads < len(chunks)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,stall,expected",
    [(200, False, "PARSE"), (200, True, "NETWORK"), (429, True, "RATE_LIMITED")],
)
def test_ticket_stream_is_closed_on_limit_timeout_or_rate_limit(
    harness, monkeypatch, status, stall, expected
):
    from ticket_watcher import transport as module

    monkeypatch.setattr(module, "PUBLIC_BODY_LIMIT", 128)
    w = harness["watcher"]
    w.transport.config = replace(harness["config"], timeout=0.03)
    stream = Chunks([b"a" * 64] * (1 if stall else 20), stall=stall)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, headers={"Retry-After": "4000"}, stream=stream)
            )
        ) as client:
            w.transport.client = client
            w.transport.owner = "stream-test"
            w.store.acquire("stream-test", harness["clock"](), 180)
            with pytest.raises(SourceError) as error:
                await asyncio.wait_for(w.transport.get_json("https://example.invalid/", {}), 1)
            assert error.value.code == expected
            if status == 429:
                assert error.value.retry_after == 4000 and stream.reads == 0
        assert stream.closed and stream.reads <= 3

    asyncio.run(scenario())


@pytest.mark.parametrize("status,stall", [(200, False), (429, False), (429, True)])
def test_discord_bounds_ack_and_preserves_rate_limit_header(harness, monkeypatch, status, stall):
    h = harness
    enable(h, monkeypatch)
    check(h, "SOLD_OUT")
    h["watcher"].notifier.config = replace(h["config"], timeout=0.03)
    stream = Chunks([b"a" * (64 if stall else 65_537)], stall=stall)
    h["responses"].append(httpx.Response(status, headers={"Retry-After": "4000"}, stream=stream))
    result = check(h, "AVAILABLE")
    assert result.data["notification"]["status"] != "SENT"
    assert stream.closed and len(h["requests"]) == 1
    if status == 429:
        assert result.data["notification"]["last_error"] == "RATE_LIMITED"
        assert h["watcher"].store.platform("discord")["blocked_until"] >= h["clock"]() + 4000
    else:
        assert result.data["notification"]["last_error"] == "INVALID_RESPONSE_BODY"


def test_health_uses_read_only_snapshot_without_mutating_database(harness):
    h, w = harness, harness["watcher"]
    w.store.connection.execute("INSERT INTO runtime VALUES('heartbeat',?)", (str(h["clock"]()),))
    before = w.store.connection.total_changes
    version = w.store.data_version()
    assert read_health(h["config"], h["clock"]).data == w.health().data
    assert read_health(h["config"], h["clock"]).data["process_healthy"]
    assert w.store.connection.total_changes == before and w.store.data_version() == version
    h["clock"].advance(120)
    assert not read_health(h["config"], h["clock"]).data["process_healthy"]


def test_health_missing_storage_does_not_create_it(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("app:\n  database_path: absent/watcher.db\ntargets: []\n")
    assert main(["--config", str(path), "health"]) == 1
    assert json.loads(capsys.readouterr().out)["execution_status"] == "FAILED"
    assert not (tmp_path / "absent").exists()


def test_health_does_not_migrate_old_schema(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError, match="版本"):
        read_health(Config(database_path=path))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0


def test_health_cli_does_not_import_worker_or_http_clients(tmp_path):
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
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == "[]"


def test_public_origin_supports_tunnel_without_trusting_forwarded_headers(tmp_path):
    async def scenario():
        async with panel(tmp_path, public_origin="https://tickets.example.com/") as (client, c, _):
            headers = {
                "Host": "tickets.example.com",
                "Origin": "https://tickets.example.com",
                "Sec-Fetch-Site": "same-origin",
            }
            response = await client.get("/api/state", headers=headers)
            assert response.status == 200
            state = await response.json()
            response = await client.post(
                "/api/settings",
                headers={**headers, "X-CSRF-Token": state["csrf"]},
                json={"revision": state["revision"], "value": {}},
            )
            assert response.status == 200
            assert (await client.post("/api/settings", headers=headers, json={})).status == 403
            for bad in (
                {**headers, "Origin": "http://tickets.example.com"},
                {**headers, "Origin": "https://attacker.invalid"},
                {**headers, "Host": "tickets.example.com.attacker.invalid"},
                {**headers, "Sec-Fetch-Site": "cross-site"},
                {
                    "Host": "attacker.invalid",
                    "X-Forwarded-Host": "tickets.example.com",
                    "X-Forwarded-Proto": "https",
                },
            ):
                assert (await client.get("/api/state", headers=bad)).status == 403
            assert (await client.get("/api/state")).status == 200
        async with panel(tmp_path) as (client, _, _):
            assert (await client.get("/api/state", headers=headers)).status == 403

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "origin",
    [
        "http://tickets.example.com",
        "https://user@tickets.example.com",
        "https://tickets.example.com/path",
        "https://tickets.example.com?x=y",
        "https://tickets.example.com:99999",
    ],
)
def test_invalid_public_origin_fails_before_creating_files(tmp_path, origin):
    with pytest.raises(ValueError):
        create_app(tmp_path / "ui.yaml", public_origin=origin)
    assert not (tmp_path / "ui.yaml").exists()


def test_public_origin_environment_and_cli_override(monkeypatch):
    monkeypatch.setenv("TICKET_WATCHER_PUBLIC_ORIGIN", "https://tickets.example.com")
    assert parser().parse_args(["ui"]).public_origin == "https://tickets.example.com"
    assert (
        parser().parse_args(["ui", "--public-origin", "https://other.example.com"]).public_origin
        == "https://other.example.com"
    )


def test_ui_view_refresh_skips_unneeded_log_reads(tmp_path, monkeypatch):
    async def scenario():
        async with panel(tmp_path) as (client, c, _):
            calls = []
            recent = c.watcher.query_log.recent
            monkeypatch.setattr(
                c.watcher.query_log, "recent", lambda: (calls.append("logs"), recent())[1]
            )
            for view in ("targets", "channels", "settings", "events"):
                response = await client.get("/api/state", params={"view": view})
                assert response.status == 200
                value = await response.json()
                assert "query_logs" not in value
                assert ("events" in value) == (view == "events")
            assert calls == []
            response = await client.get("/api/state?view=logs")
            assert "query_logs" in await response.json() and calls == ["logs"]
            assert "query_logs" in await snapshot(client)  # Keep the full API compatible.
            assert (await client.get("/api/state?view=invalid")).status == 400

    asyncio.run(scenario())
