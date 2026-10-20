"""Bound fast polling without treating stock quantity changes as renewed urgency."""

from .models import TicketStatus


def evaluate_policy(config, old, state, items, *, now, complete, trigger, signal):
    state = dict(state)
    complete = (
        complete and bool(items) and all(item.status != TicketStatus.UNKNOWN for item in items)
    )
    if old.get("last_error"):
        state.update(
            fingerprint=None, stable_since=None, stable_checks=0, supply_since=None, supply_checks=0
        )
    active = old["mode"] == "ACTIVE"
    started = state.get("active_started_at")
    if active and started is None:
        started = (old["active_until"] or now) - config.active_window
    fingerprint = sorted([item.item_key, item.status.value] for item in items)
    available = any(item.status == TicketStatus.AVAILABLE for item in items)
    no_available = old["no_available"]
    if complete:
        no_available = 0 if available or signal else no_available + 1
        if fingerprint and fingerprint == state.get("fingerprint"):
            state["stable_checks"] = state.get("stable_checks", 0) + 1
        else:
            state.update(stable_since=now, stable_checks=1)
        state["fingerprint"] = fingerprint
        if available:
            if state.get("supply_since") is None:
                state["supply_since"] = now
            state["supply_checks"] = state.get("supply_checks", 0) + 1
        elif not signal:
            state.update(supply_since=None, supply_checks=0)
    else:
        # An observation gap is not evidence of stable or continuous inventory.
        state.update(
            fingerprint=None, stable_since=None, stable_checks=0, supply_since=None, supply_checks=0
        )

    cooling = now < state.get("cooldown_until", 0)
    # A persistent hint cannot restart fast mode when cooldown expires. Require
    # a fresh transition, or a new episode after complete no-signal observations.
    if complete and no_available >= config.exit_active_checks:
        state["suppressed"] = False
    may_enter = not cooling and (trigger or (signal and not state.get("suppressed")))
    if not active and may_enter:
        active, started = True, now
        state["reason"] = "NEW_SIGNAL"
        state.update(
            stable_since=now,
            stable_checks=1 if complete else 0,
            supply_since=now if complete and available else None,
            supply_checks=1 if complete and available else 0,
        )

    reason = None
    if active:
        if now >= started + config.active_window:
            reason = "ACTIVE_LIMIT"
        elif complete and no_available >= config.exit_active_checks:
            reason = "NO_STOCK"
        elif complete:
            for prefix, label in (("supply", "SUSTAINED_AVAILABILITY"), ("stable", "UNCHANGED")):
                since = state.get(f"{prefix}_since")
                if (
                    since is not None
                    and now - since >= config.active_stable_seconds
                    and state.get(f"{prefix}_checks", 0) >= config.active_stable_checks
                ):
                    reason = label
                    break
    if reason:
        active = False
        state["reason"] = reason
        if reason != "NO_STOCK":
            state.update(cooldown_until=now + config.active_cooldown, suppressed=True)
    state["active_started_at"] = started if active else None
    return (
        state,
        "ACTIVE" if active else "NORMAL",
        (started + config.active_window if active else None),
        no_available,
        reason,
    )
