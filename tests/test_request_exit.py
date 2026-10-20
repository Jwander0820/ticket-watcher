import asyncio
import random
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import URL, Clock, Source

from ticket_watcher.config import Config, Target, parse_config
from ticket_watcher.service import Watcher
from ticket_watcher.transport import PublicTransport


class High:
    def randint(self, low, high):
        return high


@pytest.mark.parametrize(("value", "expected"), [(None, (5, 15)), (8, (8, 8)), ([6, 20], (6, 20))])
def test_request_gap_accepts_range_and_legacy_number(value, expected):
    http = {} if value is None else {"min_request_gap_seconds": value}
    assert parse_config({"http": http}, Path(".")).request_gap == expected


@pytest.mark.parametrize("value", [[3, 10], [15, 5], [5], 4, True])
def test_invalid_request_gap_fails(value):
    with pytest.raises(ValueError):
        parse_config({"http": {"min_request_gap_seconds": value}}, Path("."))


@pytest.mark.parametrize("status", [200, 503])
def test_request_gap_starts_after_the_request_finishes(harness, status):
    h, store = harness, harness["watcher"].store

    def handle(request):
        h["clock"].advance(7)  # a slow response
        return httpx.Response(status, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    config = replace(h["config"], request_gap=(5, 15))
    transport = PublicTransport(client, store, config, h["clock"], High())
    transport.owner = "owner"
    store.acquire("owner", h["clock"](), 180)
    started = h["clock"]()

    async def request():
        try:
            await transport.get_json("https://apis.ticketplus.com.tw/x", {})
        except Exception:
            pass
        finally:
            await client.aclose()

    asyncio.run(request())
    assert store.platform()["next_request"] == started + 7 + 15


@pytest.fixture
def crowd(tmp_path):
    clock = Clock()
    targets = tuple(Target(ident, ident, URL) for ident in ("a", "b", "c"))
    config = Config(
        database_path=tmp_path / "state.db",
        targets=targets,
        webhook_url_env="TICKET_WATCHER_TEST_WEBHOOK",
        system_alerts=False,
        normal_interval=(300, 300),
        active_interval=(60, 60),
    )
    source, order = Source(clock), []

    class Recorder:
        async def fetch(self, target):
            order.append(target.id)
            source.push("SOLD_OUT")
            return await source.fetch(target)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    watcher = Watcher(config, client=client, clock=clock, rng=random.Random(7), adapter=Recorder())
    asyncio.run(watcher.tick())  # new targets keep configuration order
    assert order == ["a", "b", "c"]
    order.clear()
    yield watcher, clock, order
    watcher.store.close()


def schedule(watcher, **states):
    for ident, (mode, next_check) in states.items():
        watcher.store.connection.execute(
            "UPDATE targets SET mode=?,next_check=? WHERE id=?", (mode, next_check, ident)
        )


def test_due_checks_run_fast_mode_first_then_most_overdue(crowd):
    watcher, clock, order = crowd
    now = clock()
    schedule(
        watcher,
        a=("NORMAL", now - 50),
        b=("NORMAL", now - 100),
        c=("ACTIVE", now - 1),
    )
    asyncio.run(watcher.tick())
    assert order == ["c", "b", "a"]


def test_fast_target_due_mid_round_jumps_ahead_of_normal(crowd):
    watcher, clock, order = crowd
    now = clock()
    schedule(
        watcher,
        a=("NORMAL", now - 100),
        b=("NORMAL", now - 50),
        c=("ACTIVE", now + 5),  # becomes due while "a" is being checked
    )
    fetch = watcher.adapter.fetch

    async def slow(target):
        clock.advance(10)
        return await fetch(target)

    watcher.adapter.fetch = slow
    asyncio.run(watcher.tick())
    assert order == ["a", "c", "b"]


def test_recovery_spread_schedules_fast_targets_first(crowd):
    watcher, clock, _ = crowd
    now = clock()
    schedule(
        watcher,
        a=("NORMAL", now - 100),
        b=("ACTIVE", now - 1),
        c=("NORMAL", now - 50),
    )
    watcher.prepare_monitoring()
    due = {ident: watcher.store.target(ident)["next_check"] for ident in "abc"}
    assert due["b"] < due["a"] and due["b"] < due["c"]
