"""Persist one recovery attempt per unexplained HTTP 403 episode."""

import hashlib
import json

from .models import timestamp

KEY = "ticketplus_blocked_recovery"
WAIT_SECONDS = (12 * 60 * 60, 24 * 60 * 60)


def eligible(diagnostic):
    # Incomplete diagnostics cannot rule out a challenge. Generic access-denied
    # and CloudFront pages are evidence of rejection, not of its underlying cause.
    return (
        diagnostic.get("status") == 403
        and diagnostic.get("body_read") == "complete"
        and not diagnostic.get("cf_mitigated")
        and not diagnostic.get("aws_waf_action")
        and not set(diagnostic.get("body_signals", ()))
        & {"captcha", "cloudflare_challenge", "geo_restriction", "rate_limited"}
    )


def route_key(config):
    return hashlib.sha256(config.ticketplus_proxy.encode()).hexdigest()


def read(connection):
    row = connection.execute("SELECT value FROM runtime WHERE key=?", (KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def write(connection, state):
    if state is None:
        connection.execute("DELETE FROM runtime WHERE key=?", (KEY,))
    else:
        connection.execute("INSERT OR REPLACE INTO runtime VALUES(?,?)", (KEY, json.dumps(state)))


def summary(connection):
    state = read(connection)
    if state is None:
        return None
    return {
        "status": state["status"],
        "target_id": state["target_id"],
        "retry_at": timestamp(state["retry_at"]),
        "attempted_at": timestamp(state.get("attempted_at")),
        "finished_at": timestamp(state.get("finished_at")),
        "reason": state.get("reason"),
    }
