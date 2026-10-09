"""Notification snapshots and durable delivery through the real state machine."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime

import httpx
import pytest
from test_multichannel import configure
from test_notifications import enable

from ticket_watcher.models import SourceError, TicketItem, TicketStatus
from ticket_watcher.notification_text import discord_length
from ticket_watcher.notifications import DiscordNotifier
from ticket_watcher.storage import Store

EVENT = "YUURI 2026 LIVE IN TAIPEI"
ORDER = "https://ticketplus.com.tw/order/event/session"


@pytest.fixture
def compact(harness):
    h = harness
    h["clock"].now = datetime.fromisoformat("2026-10-09T15:13:27+08:00").timestamp()

    class InnerSource:
        async def fetch(self, target):
            return replace(
                await h["source"].fetch(target),
                event_name=EVENT,
                public_url=ORDER,
                granularity="AREA",
            )

    h["watcher"].adapter = InnerSource()

    def observe(*statuses, sessions=None, counts=None, names=None, granularity="AREA"):
        state = h["watcher"].store.target("test")
        if state:
            h["clock"].now = max(h["clock"](), state["next_check"])
        items = tuple(
            TicketItem(
                f"a{i}",
                sessions[i] if sessions else "s1",
                names[i] if names else f"紅{i + 1}A區4280",
                TicketStatus(status),
                ORDER,
                date="2026-10-09 ~ 2026-10-09",
                time="17:00 ~ 17:00",
                price=4280,
                remaining_count=(counts[i] if counts else 1) if status == "AVAILABLE" else 0,
                availability_text="熱賣中" if counts and counts[i] > 20 else None,
                order_url=f"https://ticketplus.com.tw/order/event/{sessions[i]}"
                if sessions
                else ORDER,
            )
            for i, status in enumerate(statuses)
        )
        h["source"].values.append((items, "UNKNOWN" not in statuses, "inner"))
        if granularity == "PRODUCT":

            class ProductSource(InnerSource):
                async def fetch(self, target):
                    return replace(await super().fetch(target), granularity="PRODUCT")

            h["watcher"].adapter = ProductSource()
        return asyncio.run(h["watcher"].check("test"))

    return h, observe


def contents(h):
    return [json.loads(request.content)["content"] for request in h["requests"]]


def test_mixed_notice_includes_unchanged_signals_and_sends_once(compact, monkeypatch):
    h, observe = compact
    enable(h, monkeypatch)
    observe("SOLD_OUT", "SOLD_OUT", "AVAILABLE", "TEMPORARILY_UNAVAILABLE", "SOLD_OUT")
    result = observe(
        "AVAILABLE", "TEMPORARILY_UNAVAILABLE", "AVAILABLE", "TEMPORARILY_UNAVAILABLE", "SOLD_OUT"
    )
    assert result.data["evaluation"] == {
        "performed": True,
        "release_detected": True,
        "release_hint_detected": True,
    }
    assert result.data["event_id"] == result.data["hint_event_id"]
    assert (
        result.data["notification"]["status"]
        == result.data["hint_notification"]["status"]
        == "SENT"
    )
    assert contents(h) == [
        "🟢 有票｜YUURI 2026 LIVE IN TAIPEI 10/9 17:00\n"
        "紅1A區｜$4,280｜剩 1 張\n紅3A區｜$4,280｜剩 1 張\n"
        "紅2A區｜$4,280｜暫無票券\n紅4A區｜$4,280｜暫無票券\n"
        "偵測時間：10/9 15:14:27\n" + f"[前往購票]({ORDER})"
    ]
    observe(
        "AVAILABLE", "TEMPORARILY_UNAVAILABLE", "AVAILABLE", "TEMPORARILY_UNAVAILABLE", "SOLD_OUT"
    )
    assert len(h["requests"]) == 1


def test_new_hint_with_unchanged_available_has_green_title(compact, monkeypatch):
    h, observe = compact
    enable(h, monkeypatch)
    observe("AVAILABLE", "SOLD_OUT")
    result = observe("AVAILABLE", "TEMPORARILY_UNAVAILABLE")
    assert not result.data["evaluation"]["release_detected"]
    assert result.data["evaluation"]["release_hint_detected"]
    assert len(contents(h)) == 1 and contents(h)[0].startswith("🟢 有票")
    assert "暫無票券" in contents(h)[0]


def test_hint_only_and_product_labels(compact, monkeypatch):
    h, observe = compact
    enable(h, monkeypatch)
    observe("SOLD_OUT", "SOLD_OUT", names=["一般票", "學生票"], granularity="PRODUCT")
    observe(
        "TEMPORARILY_UNAVAILABLE",
        "TEMPORARILY_UNAVAILABLE",
        names=["一般票", "學生票"],
        granularity="PRODUCT",
    )
    assert len(contents(h)) == 1
    assert contents(h)[0].startswith("🟡 暫無票券")
    assert "一般票｜$4,280｜暫無票券\n學生票｜$4,280｜暫無票券" in contents(h)[0]


@pytest.mark.parametrize("remaining", ["AVAILABLE", "TEMPORARILY_UNAVAILABLE"])
def test_mixed_queue_keeps_surviving_trigger_and_cancels_after_all_disappear(
    compact, monkeypatch, remaining
):
    h, observe = compact
    observe("SOLD_OUT", "SOLD_OUT")
    result = observe("AVAILABLE", "TEMPORARILY_UNAVAILABLE")
    statuses = (
        ("AVAILABLE", "SOLD_OUT")
        if remaining == "AVAILABLE"
        else ("SOLD_OUT", "TEMPORARILY_UNAVAILABLE")
    )
    observe(*statuses)
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(contents(h)) == 1
    assert contents(h)[0].startswith("🟢" if remaining == "AVAILABLE" else "🟡")
    assert ("紅1A區" if remaining == "AVAILABLE" else "紅2A區") in contents(h)[0]
    assert ("紅2A區" if remaining == "AVAILABLE" else "紅1A區") not in contents(h)[0]
    assert h["watcher"].store.notification_status(result.data["event_id"])["status"] == "SENT"


def test_unchanged_context_does_not_keep_expired_trigger_alive(compact, monkeypatch):
    h, observe = compact
    observe("AVAILABLE", "SOLD_OUT")
    result = observe("AVAILABLE", "TEMPORARILY_UNAVAILABLE")
    observe("AVAILABLE", "SOLD_OUT")
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert not h["requests"]
    assert (
        h["watcher"].store.notification_status(result.data["hint_event_id"])["status"]
        == "CANCELLED"
    )


def test_unknown_context_not_presented_as_current_inventory(compact, monkeypatch):
    h, observe = compact
    enable(h, monkeypatch)
    observe("AVAILABLE", "SOLD_OUT")
    observe("UNKNOWN", "TEMPORARILY_UNAVAILABLE")
    assert len(contents(h)) == 1 and contents(h)[0].startswith("🟡")
    assert "紅1A區" not in contents(h)[0]


def test_sessions_have_separate_links_and_aggregate_delivery_status(compact, monkeypatch):
    h, observe = compact
    enable(h, monkeypatch)
    observe("SOLD_OUT", "SOLD_OUT", sessions=["s1", "s2"])
    result = observe("AVAILABLE", "TEMPORARILY_UNAVAILABLE", sessions=["s1", "s2"])
    assert len(contents(h)) == 2
    assert "紅1A區" in contents(h)[0] and "/s1)" in contents(h)[0]
    assert "紅2A區" in contents(h)[1] and "/s2)" in contents(h)[1]
    assert all(text.count("[前往購票]") == 1 for text in contents(h))
    assert (
        result.data["notification"]["status"]
        == result.data["hint_notification"]["status"]
        == "SENT"
    )


def test_long_snapshot_splits_without_omitting_rows_or_repeating_successful_parts(
    compact, monkeypatch
):
    h, observe = compact
    enable(h, monkeypatch)
    names = [f"第{i:03d}區" + "長名稱" * 15 for i in range(80)]
    observe(*(["SOLD_OUT"] * 80), names=names)
    h["responses"].extend([httpx.Response(200, json={"id": "first"}), httpx.Response(500)])
    result = observe(*(["AVAILABLE"] * 40 + ["TEMPORARILY_UNAVAILABLE"] * 40), names=names)
    assert result.data["notification"]["status"] == "PENDING"
    assert len(result.data["event_ids"]) > 1
    failed = contents(h)[1]
    first = contents(h)[0]
    h["clock"].advance(10)
    other = Store(h["config"].database_path)
    try:
        asyncio.run(
            DiscordNotifier(h["client"], other, h["config"], h["clock"]).deliver(max_messages=100)
        )
    finally:
        other.close()
    messages = contents(h)
    assert all(discord_length(text) <= 2000 for text in messages)
    assert messages.count(first) == 1 and messages.count(failed) == 2
    delivered = messages[:1] + messages[2:]
    for name in names:
        assert sum(name in text for text in delivered) == 1
    assert h["watcher"].store.notification_status(result.data["event_ids"])["status"] == "SENT"


def test_multichannel_mixed_notification_retries_only_failed_channel(compact):
    h, observe = compact
    configure(h)
    observe("SOLD_OUT", "SOLD_OUT")
    h["responses"].extend([httpx.Response(200, json={"id": "first"}), httpx.Response(500)])
    result = observe("AVAILABLE", "TEMPORARILY_UNAVAILABLE")
    assert len(contents(h)) == 2 and contents(h)[0] == contents(h)[1]
    h["clock"].advance(10)
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(contents(h)) == 3
    assert h["requests"][0].url != h["requests"][-1].url
    assert h["watcher"].store.notification_status(result.data["event_id"])["status"] == "SENT"


def test_unlimited_or_large_inventory_is_not_an_invented_exact_count(compact, monkeypatch):
    h, observe = compact
    enable(h, monkeypatch)
    observe("SOLD_OUT")
    observe("AVAILABLE", counts=[999999])
    assert "熱賣中" in contents(h)[0] and "999999" not in contents(h)[0]


@pytest.mark.parametrize(
    "code,title,body",
    [
        ("NETWORK", "⚠️ 查詢異常", "網路連線失敗，稍後自動重試。"),
        ("RATE_LIMITED", "⚠️ 查詢異常", "TicketPlus 限制查詢頻率，稍後自動重試。"),
        ("PARSE", "⚠️ 查詢異常", "無法解析票況，稍後自動重試。"),
        ("UNSUPPORTED", "⛔ 查詢已暫停", "不支援此資料來源，需人工檢查。"),
        ("BLOCKED", "⛔ 查詢已暫停", "TicketPlus 拒絕存取或要求驗證，需人工處理。"),
    ],
)
def test_system_error_uses_concise_title_and_activity(compact, monkeypatch, code, title, body):
    h, observe = compact
    enable(h, monkeypatch)
    w = h["watcher"]
    w.config = w.notifier.config = replace(w.config, system_alerts=True)
    observe("SOLD_OUT")
    h["clock"].now = w.store.target("test")["next_check"]
    h["source"].values.append(SourceError(code, "private diagnostic detail"))
    asyncio.run(w.check("test"))
    assert contents(h) == [f"{title}｜{EVENT}\n{body}"]
    if code == "NETWORK":
        observe("SOLD_OUT")
        assert contents(h)[-1] == f"✅ 查詢恢復｜{EVENT}\n已恢復取得完整票況。"


def test_partial_data_notice_then_pause_remains_concise(compact, monkeypatch):
    h, observe = compact
    enable(h, monkeypatch)
    w = h["watcher"]
    w.config = w.notifier.config = replace(w.config, system_alerts=True)
    observe("SOLD_OUT")
    for _ in range(3):
        observe("UNKNOWN")
    assert contents(h) == [
        f"⚠️ 票況不完整｜{EVENT}\n部分票況無法確認，保留上次有效紀錄，稍後重試。",
        f"⛔ 查詢已暫停｜{EVENT}\n連續無法解析完整票況，需人工檢查。",
    ]


def test_cancelled_snapshot_context_does_not_reappear_in_old_notification(compact, monkeypatch):
    h, observe = compact
    observe("AVAILABLE", "SOLD_OUT")
    old = observe("AVAILABLE", "TEMPORARILY_UNAVAILABLE")
    observe("SOLD_OUT", "TEMPORARILY_UNAVAILABLE")
    observe("AVAILABLE", "TEMPORARILY_UNAVAILABLE")
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(contents(h)) == 2
    assert "紅1A區" not in contents(h)[0]
    assert "紅1A區" in contents(h)[1]
    assert h["watcher"].store.notification_status(old.data["hint_event_id"])["status"] == "SENT"
