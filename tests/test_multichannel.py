import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from conftest import URL
from test_deploy_state import deploy_state
from test_monitoring import check
from test_web import mutate, panel, snapshot

from ticket_watcher.config import Channel, load_config, parse_config
from ticket_watcher.notifications import DiscordNotifier
from ticket_watcher.private_io import atomic_text
from ticket_watcher.storage import Store

HOOK_A = "https://discord.com/api/webhooks/123/token_a"
HOOK_B = "https://discord.com/api/webhooks/456/token_b"


def configure(h, *, credentials=True):
    config = replace(
        h["config"],
        channels=(Channel("a", "A"), Channel("b", "B"), Channel("c", "C")),
        targets=(replace(h["config"].targets[0], channel_ids=("a", "b")),),
    )
    h["watcher"].config = h["watcher"].notifier.config = config
    if credentials:
        atomic_text(config.secrets_path, json.dumps({"a": HOOK_A, "b": HOOK_B}))
    return config


def deliveries(watcher, event_id):
    return {
        row["channel_id"]: row for row in watcher.store.notification_status(event_id)["deliveries"]
    }


@pytest.mark.parametrize("kind", ["target", "worker"])
@pytest.mark.parametrize("value", [[], "a", None, ["a", "a"], ["absent"], [1], [["a"]]])
def test_channel_selection_rejects_empty_duplicate_or_invalid_values(tmp_path, kind, value):
    document = {"channels": [{"id": "a", "name": "A"}], "targets": [{"id": "t", "url": URL}]}
    if kind == "target":
        document["targets"][0]["channel_ids"] = value
    else:
        document["notifications"] = {"worker_alert_channel_ids": value}
    with pytest.raises(ValueError):
        parse_config(document, tmp_path)


def test_legacy_config_and_multi_config_preserve_defaults_and_signature(tmp_path):
    document = {"channels": [{"id": "a", "name": "A"}], "targets": [{"id": "t", "url": URL}]}
    old = parse_config(document, tmp_path)
    assert old.targets[0].notification_channels == ("default",)
    assert old.worker_notification_channels == ("default",)
    document["targets"][0]["channel_id"] = "a"
    document["notifications"] = {"worker_alert_channel_id": "a"}
    legacy = parse_config(document, tmp_path)
    assert legacy.targets[0].notification_channels == ("a",)
    assert legacy.worker_notification_channels == ("a",)
    document["targets"][0]["channel_ids"] = ["a", "default"]
    with pytest.raises(ValueError):
        parse_config(document, tmp_path)
    del document["targets"][0]["channel_id"]
    document["notifications"] = {"worker_alert_channel_ids": ["default", "a"]}
    multi = parse_config(document, tmp_path)
    assert multi.targets[0].notification_channels == ("a", "default")
    assert multi.targets[0].signature == old.targets[0].signature
    assert multi.worker_notification_channels == ("default", "a")
    document["notifications"]["worker_alert_channel_id"] = "a"
    with pytest.raises(ValueError):
        parse_config(document, tmp_path)


@pytest.mark.parametrize(
    "status,kind", [("AVAILABLE", "RELEASE"), ("TEMPORARILY_UNAVAILABLE", "RELEASE_HINT")]
)
def test_one_ticket_event_fans_out_without_duplicate_event_or_repeat(harness, status, kind):
    h = harness
    configure(h)
    check(h, "SOLD_OUT")
    result = check(h, status)
    key = "notification" if kind == "RELEASE" else "hint_notification"
    assert result.data[key]["status"] == "SENT"
    assert len(h["requests"]) == 2
    assert {str(r.url).split("?")[0] for r in h["requests"]} == {HOOK_A, HOOK_B}
    assert len({json.loads(r.content)["content"] for r in h["requests"]}) == 1
    page = h["watcher"].events(limit=1).data
    assert page["total"] == 1 and page["next_offset"] is None
    assert page["events"][0]["kind"] == kind
    assert len(page["events"][0]["deliveries"]) == 2
    check(h, status)
    assert len(h["requests"]) == 2


def test_retry_after_restart_only_resends_failed_destination(harness):
    h = harness
    config = configure(h)
    check(h, "SOLD_OUT")
    h["responses"].extend([httpx.Response(200, json={"id": "a-message"}), httpx.Response(500)])
    result = check(h, "AVAILABLE")
    event_id = result.data["event_id"]
    assert result.data["notification"]["status"] == "PENDING"
    assert deliveries(h["watcher"], event_id)["a"]["status"] == "SENT"
    assert h["watcher"].health().data["pending_notifications"] == 1
    h["clock"].advance(10)
    other = Store(config.database_path)
    try:
        asyncio.run(DiscordNotifier(h["client"], other, config, h["clock"]).deliver())
    finally:
        other.close()
    assert len(h["requests"]) == 3 and str(h["requests"][-1].url).split("?")[0] == HOOK_B
    statuses = deliveries(h["watcher"], event_id)
    assert statuses["a"]["attempts"] == 1 and statuses["b"]["attempts"] == 2
    assert statuses["a"]["message_id"] == "a-message"
    assert h["watcher"].store.notification_status(event_id)["status"] == "SENT"


def test_terminal_failure_is_partial_delivery_and_does_not_resend_success(harness):
    h = harness
    configure(h)
    check(h, "SOLD_OUT")
    h["responses"].extend([httpx.Response(200, json={"id": "a-message"}), httpx.Response(404)])
    result = check(h, "AVAILABLE")
    assert result.data["notification"]["status"] == "PARTIAL"
    assert deliveries(h["watcher"], result.data["event_id"])["b"]["status"] == "FAILED"
    h["clock"].advance(30)
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(h["requests"]) == 2


def test_unconfigured_channel_does_not_block_configured_channel_or_spend_attempts(harness):
    h = harness
    config = configure(h, credentials=False)
    atomic_text(config.secrets_path, json.dumps({"b": HOOK_B}))
    check(h, "SOLD_OUT")
    result = check(h, "AVAILABLE")
    event_id = result.data["event_id"]
    statuses = deliveries(h["watcher"], event_id)
    assert statuses["a"]["status"] == "PENDING" and statuses["a"]["attempts"] == 0
    assert statuses["b"]["status"] == "SENT" and len(h["requests"]) == 1
    assert h["watcher"].notifier.next_delivery_at() == h["clock"]() + 600
    h["clock"].advance(601)
    asyncio.run(h["watcher"].notifier.deliver())
    assert deliveries(h["watcher"], event_id)["a"]["status"] == "EXPIRED"
    assert h["watcher"].store.notification_status(event_id)["status"] == "PARTIAL"


def test_deselect_cancels_only_removed_channel_without_retroactive_send(harness):
    h = harness
    config = configure(h, credentials=False)
    check(h, "SOLD_OUT")
    result = check(h, "AVAILABLE")
    event_id = result.data["event_id"]
    before = h["watcher"].store.items("test")
    updated = replace(config, targets=(replace(config.targets[0], channel_ids=("a", "c")),))
    h["watcher"].store.cancel_obsolete_notices(updated, h["clock"]())
    statuses = deliveries(h["watcher"], event_id)
    assert statuses["a"]["status"] == "PENDING" and statuses["b"]["status"] == "CANCELLED"
    assert "c" not in statuses
    h["watcher"].config = h["watcher"].notifier.config = config
    h["watcher"].store.cancel_obsolete_notices(config, h["clock"]())
    atomic_text(config.secrets_path, json.dumps({"a": HOOK_A, "b": HOOK_B}))
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(h["requests"]) == 1 and str(h["requests"][0].url).split("?")[0] == HOOK_A
    assert h["watcher"].store.items("test") == before


def test_sold_out_cancellation_preserves_successful_channel_history(harness):
    h = harness
    configure(h)
    check(h, "SOLD_OUT")
    h["responses"].extend([httpx.Response(200, json={"id": "a-message"}), httpx.Response(500)])
    result = check(h, "AVAILABLE")
    check(h, "SOLD_OUT")
    statuses = deliveries(h["watcher"], result.data["event_id"])
    assert statuses["a"]["status"] == "SENT" and statuses["a"]["message_id"] == "a-message"
    assert statuses["b"]["status"] == "CANCELLED"
    assert len(h["requests"]) == 2


def test_system_query_alerts_use_all_target_channels(harness):
    h = harness
    config = replace(configure(h), system_alerts=True)
    h["watcher"].config = h["watcher"].notifier.config = config
    h["watcher"].store.sync_target(config.targets[0])
    with h["watcher"].store.transaction() as db:
        h["watcher"]._system(db, "模擬查詢異常", "test")
    asyncio.run(h["watcher"].notifier.deliver())
    assert len(h["requests"]) == 2
    assert all("模擬查詢異常" in json.loads(r.content)["content"] for r in h["requests"])


def test_ui_multi_selection_roundtrip_delete_guards_and_worker_fanout(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            state = await snapshot(client)
            for name, hook in (("A", HOOK_A), ("B", HOOK_B)):
                response = await mutate(
                    client, state, "/api/channels", {"name": name, "webhook_url": hook}
                )
                assert response.status == 200
                state = await response.json()
            channels = [row["id"] for row in state["channels"][1:]]
            response = await mutate(
                client,
                state,
                "/api/targets",
                {"name": "多頻道", "url": URL, "channel_ids": channels},
            )
            assert response.status == 200
            state = await response.json()
            ident = state["targets"][0]["id"]
            assert state["targets"][0]["channel_ids"] == channels
            assert load_config(c.path).targets[0].notification_channels == tuple(channels)
            for channel in channels:
                assert (
                    await mutate(client, state, f"/api/channels/{channel}", method="DELETE")
                ).status == 409
            for values in ([], [channels[0], channels[0]], ["missing"]):
                assert (
                    await mutate(
                        client,
                        state,
                        f"/api/targets/{ident}",
                        {"name": "多頻道", "channel_ids": values},
                        "PUT",
                    )
                ).status == 400
            response = await mutate(
                client,
                state,
                "/api/settings",
                {"notifications": {"worker_alert_channel_ids": channels}},
            )
            assert response.status == 200
            state = await response.json()
            assert state["settings"]["notifications"]["worker_alert_channel_ids"] == channels
            assert load_config(c.path).worker_notification_channels == tuple(channels)
            c._worker_event("multi-worker", "模擬服務異常")
            await c.watcher.notifier.deliver(event_id="multi-worker")
            assert c.watcher.store.notification_status("multi-worker")["status"] == "SENT"
            assert len(requests) == 2
            assert HOOK_A not in json.dumps(state) and HOOK_B not in json.dumps(state)
            # Legacy API clients can still replace a multi-selection with one destination.
            state = await (
                await mutate(
                    client,
                    state,
                    f"/api/targets/{ident}",
                    {"name": "多頻道", "channel_id": "default"},
                    "PUT",
                )
            ).json()
            assert state["targets"][0]["channel_ids"] == ["default"]
            assert (
                await mutate(client, state, f"/api/channels/{channels[1]}", method="DELETE")
            ).status == 409
            state = await (
                await mutate(
                    client,
                    state,
                    "/api/settings",
                    {"notifications": {"worker_alert_channel_id": "default"}},
                )
            ).json()
            assert (
                await mutate(client, state, f"/api/channels/{channels[1]}", method="DELETE")
            ).status == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("channel", ["default", "b"])
def test_legacy_migration_preserves_delivery_fields_and_baseline(harness, version, channel):
    h = harness
    configure(h)
    # Construct the actual legacy single-destination table, including an in-flight claim.
    h["watcher"].config = h["watcher"].notifier.config = replace(
        h["watcher"].config, targets=(replace(h["config"].targets[0], channel_id=channel),)
    )
    check(h, "SOLD_OUT")
    h["responses"].append(httpx.Response(500))
    result = check(h, "AVAILABLE")
    h["clock"].advance(10)
    notice = h["watcher"].store.claim_notice(h["clock"]())
    db = h["watcher"].store.connection
    original = dict(
        db.execute("SELECT * FROM outbox WHERE event_id=?", (result.data["event_id"],)).fetchone()
    )
    with h["watcher"].store.transaction():
        db.execute("ALTER TABLE outbox RENAME TO outbox_new")
        db.execute("""CREATE TABLE outbox (
         event_id TEXT PRIMARY KEY REFERENCES events(id), status TEXT NOT NULL DEFAULT 'PENDING',
         attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, expires_at REAL NOT NULL,
         lease_until REAL, message_id TEXT, last_error TEXT, excluded_items TEXT NOT NULL DEFAULT '[]')""")
        db.execute("""INSERT INTO outbox SELECT event_id,status,attempts,next_attempt,expires_at,
         lease_until,message_id,last_error,excluded_items FROM outbox_new""")
        db.execute("DROP TABLE outbox_new")
        if version == 1:
            db.execute("ALTER TABLE outbox DROP COLUMN excluded_items")
        db.execute(f"PRAGMA user_version={version}")
    before = h["watcher"].store.items("test")
    upgraded = Store(h["config"].database_path)
    try:
        row = dict(upgraded.connection.execute("SELECT * FROM outbox").fetchone())
        assert row == original and row["channel_id"] == channel
        assert upgraded.items("test") == before
        assert upgraded.claim_notice(h["clock"]()) is None
        h["clock"].advance(121)
        recovered = upgraded.claim_notice(h["clock"]())
        assert recovered["channel_id"] == channel
        assert recovered["attempts"] == notice["attempts"] + 1
        assert not upgraded.finish_notice(notice, h["clock"](), "SENT", message_id="stale")
        assert upgraded.finish_notice(recovered, h["clock"](), "SENT", message_id="new")
        assert upgraded.notification_status(result.data["event_id"])["message_id"] == "new"
    finally:
        upgraded.close()
    assert (
        deploy_state.schema_signature(h["config"].database_path) == deploy_state.supported_schema()
    )


def test_prune_preserves_completed_channels_while_event_has_pending_delivery(harness):
    h = harness
    configure(h)
    check(h, "SOLD_OUT")
    h["responses"].extend([httpx.Response(200, json={"id": "a-message"}), httpx.Response(500)])
    result = check(h, "AVAILABLE")
    event_id = result.data["event_id"]
    store = h["watcher"].store
    store.connection.execute("UPDATE events SET created_at=?", (h["clock"]() - 31 * 86400,))
    store.prune(h["clock"](), 30)
    statuses = deliveries(h["watcher"], event_id)
    assert statuses["a"]["message_id"] == "a-message" and statuses["b"]["status"] == "PENDING"
    store.connection.execute("UPDATE outbox SET status='FAILED' WHERE channel_id='b'")
    h["clock"].advance(3600)
    store.prune(h["clock"](), 30)
    assert store.notification_status(event_id)["status"] == "NOT_REQUIRED"
    assert h["watcher"].events().data["total"] == 0


def test_removed_inflight_channel_cannot_overwrite_cancelled_delivery(harness):
    h = harness
    config = configure(h, credentials=False)
    check(h, "SOLD_OUT")
    result = check(h, "AVAILABLE")
    store = h["watcher"].store
    first = store.claim_notice(h["clock"]())
    assert first["channel_id"] == "a"
    assert store.finish_notice(first, h["clock"](), "SENT", message_id="a-message")
    second = store.claim_notice(h["clock"]())
    assert second["channel_id"] == "b"
    updated = replace(config, targets=(replace(config.targets[0], channel_ids=("a",)),))
    store.cancel_obsolete_notices(updated, h["clock"]())
    assert not store.finish_notice(second, h["clock"](), "SENT", message_id="late")
    statuses = deliveries(h["watcher"], result.data["event_id"])
    assert statuses["a"]["status"] == "SENT" and statuses["b"]["status"] == "CANCELLED"


def test_worker_deselection_preserves_other_pending_destination(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, c, requests):
            state = await snapshot(client)
            for name, hook in (("A", HOOK_A), ("B", HOOK_B)):
                state = await (
                    await mutate(
                        client, state, "/api/channels", {"name": name, "webhook_url": hook}
                    )
                ).json()
            channels = [row["id"] for row in state["channels"][1:]]
            state = await (
                await mutate(
                    client,
                    state,
                    "/api/settings",
                    {"notifications": {"worker_alert_channel_ids": channels}},
                )
            ).json()
            c._worker_event("pending-worker", "模擬服務異常")
            state = await (
                await mutate(
                    client,
                    state,
                    "/api/settings",
                    {"notifications": {"worker_alert_channel_ids": [channels[0]]}},
                )
            ).json()
            statuses = deliveries(c.watcher, "pending-worker")
            assert statuses[channels[0]]["status"] == "PENDING"
            assert statuses[channels[1]]["status"] == "CANCELLED"
            await c.watcher.notifier.deliver(event_id="pending-worker")
            assert len(requests) == 1 and str(requests[0].url).split("?")[0] == HOOK_A
            assert c.watcher.store.notification_status("pending-worker")["status"] == "PARTIAL"

    asyncio.run(scenario())
