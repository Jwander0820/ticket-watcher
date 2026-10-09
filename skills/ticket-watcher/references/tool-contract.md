# Tool contract v1.0

[Skill entry point](../SKILL.md)

Every JSON result includes `schema_version: "1.0"`, `execution_status` and `result_source`.
This contract describes CLI query/control results; the local UI has a separate internal HTTP API.

## UI and notification destinations

`ui` starts a loopback control panel and its own monitoring worker (default port 8787 and `data/ui-config.yaml`). Do not run a second `run` against the same UI-managed database. UI saves reload the worker while preserving schedules, platform cooldowns and unchanged source baselines. Manual file edits require restarting the UI.

Each target has `channel_ids` (default `[default]`, at least one distinct existing channel); the legacy single `channel_id` is accepted, but both fields cannot appear together in YAML. The default destination uses the UI-stored webhook first, otherwise `DISCORD_WEBHOOK_URL`. Named channels store only IDs/names in YAML; URLs live in the private `discord-webhooks.json` beside SQLite. Clearing the UI default restores environment fallback; blank edits preserve credentials. Each event snapshots its destinations and has independent delivery rows. Removing a destination cancels only that channel's unsent work, without resetting the observation baseline or reviving cancelled work on reselection. Newly selected channels do not receive old events. Missing credentials leave that channel pending until expiry without consuming attempts or blocking configured destinations. Only an explicit UI channel-test action queues a test message.

Notification results and events include `deliveries`, with each channel's `channel_id`, `status`, `message_id`, `attempts`, and `last_error`. Aggregate SENT means all destinations succeeded; INFLIGHT/PENDING take precedence while work remains, and PARTIAL means some destinations succeeded while the rest terminated. Aggregate `message_id` is populated only for a single delivery; aggregate `attempts` sums the channel attempts. Successful channels are not retried when another channel fails. Discord 429 retains the shared cooldown. Event pagination counts logical events, and health's pending count counts events with unfinished deliveries. SQLite schema version 3 migrates legacy delivery rows without losing stored state; VPS automatic deployment still rejects schema changes until an explicit offline migration is performed.

The UI supervises unexpected worker termination with 5/15/30/60/300-second delays (300 seconds thereafter), fresh worker connections, and preserved SQLite state. One failure event is created per incident; recovery is recorded after 60 seconds of continuous operation. `notifications.worker_alerts_enabled` and `worker_alert_channel_ids` (legacy single `worker_alert_channel_id` accepted) control these SYSTEM notices independently of target query alerts. Disabling worker notices cancels all queued destinations; removing a selected destination cancels only its queued work. Deliberate UI shutdown/reload is not a failure. CLI `run` still exits on unexpected failure for the process/container supervisor to restart. Neither mechanism clears protective platform pauses or guarantees alerts during host/network/storage outages.

## Execution and query semantics

Execution status: COMPLETED (exit 0), FAILED (2), DEFERRED (3), UNSUPPORTED (4). `health` exits 1 when it cannot confirm process health; interruption exits 130. Source: LIVE, CACHE, LOCAL. A completed partial observation still has `complete: false` and UNKNOWN items.

DEFERRED means no valid query/evaluation completed. Inspect `reason` and respect `next_allowed_at`; some requests may already have been made before a restriction or lease loss. `QUERY_SUPERSEDED` discards a stale query result without changing the current baseline or target failure counts. Late rate-limit or blocking signals still preserve shared platform protection.

`query --url <URL>` returns current availability, `evaluation.performed: false`, `release_detected: null`, and `notification.status: NOT_APPLICABLE`. It never writes monitoring baselines or creates events. Shared request controls and metadata cache are persisted.

`check --target <id>` respects the persisted schedule, compares all returned items, saves changes, and immediately attempts configured notification delivery. `changes`, `changes_total`, `changes_next_offset`, `event_ids`, `next_allowed_at` and `notification` describe confirmed-release work. `hint_event_ids`, `hint_notification` and `evaluation.release_hint_detected` describe outer/inner release hints. Legacy `event_id` and `hint_event_id` are the first ID of each list, or null. A hint alone does not set release_detected true. Both kinds use the same TTL, delivery controls and cancellation rules.

Notifications combine release and hint triggers from the same observation and session. Each triggered session includes all AVAILABLE and TEMPORARILY_UNAVAILABLE items in that observation's monitored scope, not only changed items. Mixed triggers share a RELEASE event and appear in both ID lists; hint-only triggers remain RELEASE_HINT even if unchanged available inventory makes the message title green. `changes` retains each trigger's actual current status, and `snapshot` retains the displayed observation. Long snapshots use durable parts, each with its own event ID and per-channel delivery. `notification` and `hint_notification` aggregate their full ID lists; delivery entries include `event_id` as well as `channel_id`. Aggregate `message_id` is only populated for a single delivery. Event summaries omit `snapshot`/`display_keys` and expose `snapshot_total` instead. Pending rows cancel only when all their triggering transitions have disappeared; unchanged snapshot context alone cannot keep an obsolete notice alive.

Explicit `check --now` (or Python `check(..., immediate=True)`) and the UI manual-check button bypass only the normal target schedule. Platform leases, request gaps, Retry-After/backoff, target error backoff, pauses, disabled flags and stop times still apply. Manual results update the same monitoring baseline and can create notifications. Default CLI checks and scheduled polling retain their previous behavior.

`auto_stop` defaults to true. Monitoring persists reliable public session start times interpreted in Asia/Taipei, stops notices for sessions that have started, and stops the whole target only when all selected sessions have reliable times and have started. Unknown times do not imply a stop. Explicit timezone-aware `stop_at` and automatic deadlines use the earlier value. One-off `query` neither obeys nor updates monitoring stop deadlines.

## Records and result fields

Completed/failed checks and one-off queries append an allowlisted JSON line to `<database-stem>-logs/queries.log`. Explicit deferred attempts are also recorded; automatic scheduler waits are omitted. One seven-day current cycle and one previous cycle are retained, with the cycle start persisted in SQLite. UI exposes the most recent 100 records. Records exclude URLs, credential values, names and raw exceptions. Query logs are separate from the existing event-retention policy.

Change timestamps `previous_observed_at` and `observed_at` are UTC Unix seconds in event payloads. Top-level times and cached item times are ISO 8601 UTC. `monitoring_gap` is true when valid observations are over 45 minutes apart. Event detection time is not an exact inventory update time.

`status --target <id> --detail full` reports last valid state/time separately from current observation. `events` returns compact summaries of state changes, errors, system alerts and releases with delivery status; add `--detail full` for original change payloads. `--limit` is 1–500; `--offset` starts at 0; `next_offset` null ends the page. Pagination only reduces presentation after full parsing/evaluation.

## Scheduling and recovery

`tick` completes one local scheduling round, running delivery alongside the checks and waiting for the final delivery pass before returning. Inspect each result in `checks` and `delivery`; the outer COMPLETED does not imply every source succeeded. `run` keeps polling and delivery in separate foreground tasks, so slow notifications do not delay subsequent polling rounds. Both operations update heartbeat independently every 30 seconds. `health` reads SQLite in read-only mode without creating/migrating storage or starting a Watcher. It reports heartbeat, recent target query results, platform pause and pending-notification count; it exits 1 if health cannot be confirmed, including absent/stale heartbeat, missing database or unsupported schema.

Errors: NETWORK, RATE_LIMITED, BLOCKED, PARSE, UNSUPPORTED. Requests share a minimum 5-second gap and persistent leases. 429 waits use the later of Retry-After and local backoff. Repeated schema problems pause the target; access refusal pauses the platform. Resume is a manual action and preserves cooldowns.

A complete successful query resets the shared platform failure count without clearing cooldowns or writing monitoring baselines. Cached status summaries cover all items, while detail pages are read with SQL pagination. Historical retention cleanup runs at most once per hour across callers sharing the database; delivery expiry checks remain independent of this cleanup interval.

## Delivery guarantees

Outbox: PENDING, INFLIGHT, SENT, CANCELLED, EXPIRED, FAILED, DISABLED. Stable event IDs allow identifying possible delivery duplicates after lost acknowledgements. Without a configured webhook, work remains PENDING until its default 10-minute TTL expires.

New target notification payloads include `target_signature`, binding them to the configuration that created them. Each delivery claim has a distinct owner; late responses cannot revive cancelled work or overwrite a newer claim. Already-issued HTTP requests cannot be recalled. Existing pending events without a signature remain readable and use the existing cancellation and current-target checks.

## Source interpretation

Activity URLs use SESSION and return order_url in full item detail. Order URLs use AREA if the session has ticket areas, otherwise PRODUCT. They accept matching item_ids; activity URLs reject item filters. Items add source_status, availability_text, remaining_count, session_name and order_url. remaining_count is a displayed 0-20 quantity; hot-sale values, including counts above 20, are null. Ticket names retain eligibility distinctions such as disability tickets. Individual seats and purchase success are not queried.

Outer and inner status unavailable is TEMPORARILY_UNAVAILABLE. A prior SOLD_OUT to that status emits RELEASE_HINT for SESSION, AREA or PRODUCT, and a later AVAILABLE from either no-ticket status emits RELEASE. Inner hints include the area/product name, price and order URL. First observations and unchanged states emit neither. Inner unavailable has remaining_count 0. Legacy inner SOLD_OUT baselines with availability_text 暫無票券 are interpreted as TEMPORARILY_UNAVAILABLE without emitting a new hint on upgrade. Missing or invalid limited inventory is UNKNOWN.

The public session/area/product endpoints were validated on 2026-10-05 and have no official stable inventory contract. MCP is not installed or implemented by this skill.
