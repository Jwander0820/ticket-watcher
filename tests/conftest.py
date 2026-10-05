import random
from dataclasses import replace

import httpx
import pytest

from ticket_watcher.config import Config, Target
from ticket_watcher.models import Observation, SourceError, TicketItem, TicketStatus
from ticket_watcher.service import Watcher

URL = "https://ticketplus.com.tw/activity/190c8cf3965a985d9151d912313e2fe1"


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Source:
    def __init__(self, clock):
        self.clock = clock
        self.values = []
        self.calls = 0

    def push(self, *statuses, complete=True, source="ticketplus-public-v2/session"):
        items = tuple(
            TicketItem(
                f"s{i:09d}",
                f"s{i:09d}",
                f"場次 {i}",
                TicketStatus(status),
                URL,
                "2026-11-29",
                "19:00",
                "範例場館",
            )
            for i, status in enumerate(statuses, 1)
        )
        self.values.append((items, complete, source))

    async def fetch(self, target):
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, SourceError):
            raise value
        items, complete, source = value
        return Observation(
            "範例活動", URL, self.clock(), items, source=source, complete=complete, request_count=1
        )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.delenv("TICKET_WATCHER_TEST_WEBHOOK", raising=False)
    clock = Clock()
    source = Source(clock)
    target = Target("test", "測試", URL)
    config = Config(
        database_path=tmp_path / "state.db",
        targets=(target,),
        webhook_url_env="TICKET_WATCHER_TEST_WEBHOOK",
        system_alerts=False,
        normal_interval=(300, 300),
        active_interval=(60, 60),
    )
    requests = []
    responses = []

    def handle(request):
        requests.append(request)
        if responses:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json={"id": "123456"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    watcher = Watcher(config, client=client, clock=clock, rng=random.Random(7), adapter=source)
    yield {
        "watcher": watcher,
        "clock": clock,
        "source": source,
        "config": config,
        "client": client,
        "requests": requests,
        "responses": responses,
        "replace_config": lambda **kw: replace(config, **kw),
    }
    watcher.store.close()
