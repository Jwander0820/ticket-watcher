import asyncio
import json
from dataclasses import replace

import pytest
from conftest import URL
from test_multichannel import HOOK_A
from test_web import mutate, panel, snapshot

from ticket_watcher.config import Channel, load_config, parse_config
from ticket_watcher.models import SourceError
from ticket_watcher.private_io import atomic_text
from ticket_watcher.service import Watcher


def observe(h, statuses, counts=None, *, seconds=150, complete=True):
    h["clock"].advance(seconds)
    h["source"].push(*statuses, complete=complete)
    items, valid, source = h["source"].values.pop()
    items = tuple(
        replace(item, session_id="same-session", remaining_count=count)
        for item, count in zip(items, counts or [None] * len(items), strict=True)
    )
    h["source"].values.append((items, valid, source))
    return asyncio.run(h["watcher"].check("test", immediate=True))


def test_changing_quantities_and_temporary_holds_still_downgrade(harness):
    h, w = harness, harness["watcher"]
    observe(h, ["SOLD_OUT", "SOLD_OUT"])
    for statuses, counts in [
        (["AVAILABLE", "AVAILABLE"], [3, 10]),
        (["AVAILABLE", "AVAILABLE"], [2, 4]),
        (["TEMPORARILY_UNAVAILABLE"] * 2, [0, 0]),
        (["AVAILABLE", "AVAILABLE"], [3, 8]),
        (["AVAILABLE", "AVAILABLE"], [2, 3]),
    ]:
        observe(h, statuses, counts)
        assert w.store.target("test")["mode"] == "ACTIVE"
    observe(h, ["AVAILABLE", "AVAILABLE"], [1, 2])
    assert w.store.target("test")["mode"] == "NORMAL"
    assert w.store.polling_state("test")["reason"] == "SUSTAINED_AVAILABILITY"
    # A hold and subsequent return still notify; cooldown prevents fast re-entry.
    observe(h, ["TEMPORARILY_UNAVAILABLE"] * 2)
    result = observe(h, ["AVAILABLE"] * 2, [3, 10])
    assert result.data["evaluation"]["release_detected"]
    assert w.store.target("test")["mode"] == "NORMAL"


def test_unchanged_hint_downgrades_and_does_not_restart_after_cooldown(harness):
    h, w = harness, harness["watcher"]
    for _ in range(5):
        observe(h, ["TEMPORARILY_UNAVAILABLE"])
    state = w.store.polling_state("test")
    assert state["reason"] == "UNCHANGED"
    assert w.store.target("test")["mode"] == "NORMAL"
    h["clock"].now = state["cooldown_until"] + 1
    observe(h, ["TEMPORARILY_UNAVAILABLE"])
    assert w.store.target("test")["mode"] == "NORMAL"
    # A genuinely new release after cooldown can start a new fast window.
    observe(h, ["AVAILABLE"], [3])
    assert w.store.target("test")["mode"] == "ACTIVE"


def test_count_alone_cannot_downgrade_and_new_releases_cannot_extend_limit(harness):
    h, w = harness, harness["watcher"]
    for _ in range(8):
        observe(h, ["TEMPORARILY_UNAVAILABLE"], seconds=5)
    assert w.store.target("test")["mode"] == "ACTIVE"
    expiry = w.store.target("test")["active_until"]
    observe(h, ["AVAILABLE"])
    observe(h, ["TEMPORARILY_UNAVAILABLE"])
    observe(h, ["AVAILABLE"])
    assert w.store.target("test")["active_until"] == expiry
    h["clock"].now = expiry
    observe(h, ["AVAILABLE"], seconds=0)
    assert w.store.target("test")["mode"] == "NORMAL"
    assert w.store.polling_state("test")["reason"] == "ACTIVE_LIMIT"


@pytest.mark.parametrize("failure", ["partial", "network"])
def test_failed_observations_do_not_prove_sustained_availability(harness, failure):
    h, w = harness, harness["watcher"]
    w.config = replace(w.config, active_window=7200)
    observe(h, ["SOLD_OUT"])
    for _ in range(4):
        observe(h, ["AVAILABLE"])
    if failure == "partial":
        observe(h, ["UNKNOWN"], complete=False)
    else:
        h["source"].values.append(SourceError("NETWORK", "test failure"))
        asyncio.run(w.check("test", immediate=True))
    h["clock"].now = w.store.target("test")["next_check"]
    observe(h, ["AVAILABLE"])
    assert w.store.target("test")["mode"] == "ACTIVE"
    assert w.store.polling_state("test")["supply_checks"] == 1


def test_cooldown_persists_across_restart_and_scope_changes_clear_it(harness):
    h, w = harness, harness["watcher"]
    for _ in range(5):
        observe(h, ["TEMPORARILY_UNAVAILABLE"])
    policy = w.store.polling_state("test")
    other = Watcher(w.config, client=h["client"], clock=h["clock"], adapter=h["source"])
    try:
        assert other.store.polling_state("test") == policy
        h["source"].push("AVAILABLE")
        asyncio.run(other.check("test", immediate=True))
        assert other.store.target("test")["mode"] == "NORMAL"
        other.store.sync_target(replace(w.config.targets[0], item_ids=("new-area",)))
        assert other.store.polling_state("test") == {}
    finally:
        other.store.close()


def test_mode_botlogs_go_to_default_even_when_target_uses_other_channel(harness):
    h, w = harness, harness["watcher"]
    config = replace(
        w.config,
        system_alerts=True,
        channels=(Channel("a", "Tickets"),),
        targets=(replace(w.config.targets[0], channel_ids=("a",)),),
    )
    w.config = w.notifier.config = config
    atomic_text(config.secrets_path, json.dumps({"default": HOOK_A}))
    for _ in range(5):
        observe(h, ["TEMPORARILY_UNAVAILABLE"])
    assert len(h["requests"]) == 2  # enter fast, then downgrade; never one per poll
    bodies = [json.loads(request.content)["content"] for request in h["requests"]]
    assert "進入快速模式" in bodies[0]
    assert "已回一般模式" in bodies[1] and "冷卻 30 分鐘" in bodies[1]
    assert all(str(request.url).startswith(HOOK_A) for request in h["requests"])
    # Cancellation/reload routing must also recognize the default destination.
    config.secrets_path.unlink()
    h["clock"].advance(1801)
    observe(h, ["AVAILABLE"])
    w.store.cancel_obsolete_notices(config, h["clock"]())
    row = w.store.connection.execute(
        "SELECT o.status FROM outbox o JOIN events e ON e.id=o.event_id "
        "WHERE json_extract(e.payload,'$.polling_alert')=1 ORDER BY e.created_at DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "PENDING"


@pytest.mark.parametrize(
    "field", ["active_stable_checks", "active_stable_seconds", "active_cooldown_seconds"]
)
def test_policy_config_rejects_invalid_thresholds(tmp_path, field):
    with pytest.raises(ValueError):
        parse_config({"polling": {field: 0}, "targets": [{"id": "test", "url": URL}]}, tmp_path)


def test_policy_thresholds_survive_ui_settings_save_and_reload(tmp_path):
    async def scenario():
        async with panel(tmp_path) as (client, controller, requests):
            values = {
                "active_stable_checks": 7,
                "active_stable_seconds": 900,
                "active_cooldown_seconds": 2400,
            }
            response = await mutate(
                client, await snapshot(client), "/api/settings", {"polling": values}
            )
            assert response.status == 200
            assert (await snapshot(client))["settings"]["polling"].items() >= values.items()
            response = await mutate(
                client,
                await snapshot(client),
                "/api/settings",
                {"polling": {"active_window_seconds": 1200}},
            )
            assert response.status == 200
            saved = load_config(controller.path)
            assert (
                saved.active_stable_checks,
                saved.active_stable_seconds,
                saved.active_cooldown,
            ) == (7, 900, 2400)
            assert not requests

    asyncio.run(scenario())
