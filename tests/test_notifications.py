import asyncio
import json

import httpx
from test_monitoring import check, releases

WEBHOOK = "https://discord.com/api/webhooks/123/test_token"


def enable(h, monkeypatch):
    monkeypatch.setenv("TICKET_WATCHER_TEST_WEBHOOK", WEBHOOK)


def test_release_immediately_sends_wait_true_and_saves_message_id(harness, monkeypatch):
    h = harness
    enable(h, monkeypatch)
    check(h, "SOLD_OUT", "SOLD_OUT")
    result = check(h, "AVAILABLE", "AVAILABLE")
    assert result.data["notification"]["status"] == "SENT"
    assert all(d["message_id"] == "123456" for d in result.data["notification"]["deliveries"])
    assert len(h["requests"]) == 2
    for index, request in enumerate(h["requests"], 1):
        assert request.url.params["wait"] == "true"
        body = json.loads(request.content)
        assert body["allowed_mentions"] == {"parse": []}
        assert f"場次 {index}" in body["content"]
    check(h, "AVAILABLE", "AVAILABLE")
    assert len(h["requests"]) == 2


def test_discord_failure_does_not_undo_ticket_state(harness, monkeypatch):
    h = harness
    enable(h, monkeypatch)
    check(h, "SOLD_OUT")
    h["responses"].append(httpx.Response(500))
    result = check(h, "AVAILABLE")
    assert result.data["notification"]["status"] == "PENDING"
    assert h["watcher"].store.items("test")[0]["last_valid"] == "AVAILABLE"
    h["clock"].advance(10)
    asyncio.run(h["watcher"].notifier.deliver())
    assert releases(h["watcher"])[0]["notification_status"] == "SENT"


def test_discord_429_respects_body_and_header_without_new_ticket_request(harness, monkeypatch):
    h = harness
    enable(h, monkeypatch)
    check(h, "SOLD_OUT")
    h["responses"].append(
        httpx.Response(429, headers={"Retry-After": "150"}, json={"retry_after": 180})
    )
    check(h, "AVAILABLE")
    h["clock"].advance(179)
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(h["requests"]) == 1
    h["clock"].advance(1)
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(h["requests"]) == 2
    assert h["source"].calls == 2


def test_pending_release_is_cancelled_after_confirmed_sold_out(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    check(h, "SOLD_OUT")
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert releases(h["watcher"])[0]["notification_status"] == "CANCELLED"
    assert not h["requests"]


def test_partial_notification_only_sends_still_available_items(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT", "SOLD_OUT")
    check(h, "AVAILABLE", "AVAILABLE")
    check(h, "SOLD_OUT", "AVAILABLE")
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    body = json.loads(h["requests"][0].content)
    assert "場次 1" not in body["content"] and "場次 2" in body["content"]
    assert sum(len(event["payload"]["changes"]) for event in releases(h["watcher"])) == 2


def test_expired_notification_is_not_sent(harness, monkeypatch):
    h = harness
    check(h, "SOLD_OUT")
    check(h, "AVAILABLE")
    h["clock"].advance(601)
    enable(h, monkeypatch)
    asyncio.run(h["watcher"].notifier.deliver())
    assert not h["requests"]
    assert releases(h["watcher"])[0]["notification_status"] == "EXPIRED"


def test_lost_ack_retries_keep_same_event_identity_and_hide_secret(harness, monkeypatch):
    h = harness
    enable(h, monkeypatch)
    check(h, "SOLD_OUT")
    h["responses"].append(httpx.ReadTimeout(f"failed at {WEBHOOK}"))
    check(h, "AVAILABLE")
    h["clock"].advance(10)
    asyncio.run(h["watcher"].notifier.deliver())
    messages = [json.loads(r.content)["content"] for r in h["requests"]]
    assert messages[0] == messages[1]
    assert "test_token" not in json.dumps(h["watcher"].events().to_dict())


def test_large_release_notification_is_bounded(harness, monkeypatch):
    h = harness
    enable(h, monkeypatch)
    check(h, *("SOLD_OUT" for _ in range(80)))
    check(h, *("AVAILABLE" for _ in range(80)))
    asyncio.run(h["watcher"].notifier.deliver(max_messages=100))
    messages = [json.loads(request.content)["content"] for request in h["requests"]]
    assert len(messages) == 80 and all(len(content) <= 2000 for content in messages)
    assert all(f"場次 {index} " in messages[index - 1] for index in range(1, 81))
    assert h["source"].calls == 2
    events = h["watcher"].events(detail=True, limit=100).data["events"]
    assert sum(len(event["payload"]["changes"]) for event in events) == 80


def test_ack_without_message_id_does_not_claim_sent(harness, monkeypatch):
    h = harness
    enable(h, monkeypatch)
    check(h, "SOLD_OUT")
    h["responses"].append(httpx.Response(204))
    result = check(h, "AVAILABLE")
    assert result.data["notification"]["status"] == "PENDING"
    assert result.data["notification"]["last_error"] == "ACKNOWLEDGEMENT_MISSING"
