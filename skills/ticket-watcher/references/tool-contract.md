# Tool contract v1.0

Every JSON result includes `schema_version: "1.0"`, `execution_status` and `result_source`.
This contract describes CLI query/control results; the local UI has a separate internal HTTP API.

`ui` starts a loopback control panel and its own monitoring worker (default port 8787 and `data/ui-config.yaml`). Do not run a second `run` against the same UI-managed database. UI saves reload the worker while preserving schedules, platform cooldowns and unchanged source baselines. Manual file edits require restarting the UI.

Each target has a `channel_id` (default `default`, using `DISCORD_WEBHOOK_URL`). Named channels store only IDs/names in YAML; URLs live in the private `discord-webhooks.json` beside SQLite. A notification records its destination channel when created. Changing a target's channel cancels its old unsent notifications without resetting the observation baseline. Missing channel credentials leave pending work unclaimed until expiry; they do not consume delivery attempts. Only an explicit UI channel-test action queues a test message.

Execution status: COMPLETED (exit 0), FAILED (2), DEFERRED (3), UNSUPPORTED (4). Source: LIVE, CACHE, LOCAL. A completed partial observation still has `complete: false` and UNKNOWN items.

`query --url <URL>` returns current availability, `evaluation.performed: false`, `release_detected: null`, and `notification.status: NOT_APPLICABLE`. It never writes monitoring baselines or creates events. Shared request controls and metadata cache are persisted.

`check --target <id>` respects the persisted schedule, compares all returned items, saves changes, and immediately attempts configured notification delivery. `changes`, `changes_total`, `changes_next_offset`, `event_id`, `next_allowed_at` and `notification` describe confirmed-release work. `hint_event_id`, `hint_notification` and `evaluation.release_hint_detected` separately describe outer-page release hints. A hint does not set release_detected true. Both kinds use the same TTL, delivery controls and cancellation rules.

Change timestamps `previous_observed_at` and `observed_at` are UTC Unix seconds in event payloads. Top-level times and cached item times are ISO 8601 UTC. `monitoring_gap` is true when valid observations are over 45 minutes apart. Event detection time is not an exact inventory update time.

`status --target <id> --detail full` reports last valid state/time separately from current observation. `events` returns compact summaries of state changes, errors, system alerts and releases with delivery status; add `--detail full` for original change payloads. `--limit` is 1–500; `--offset` starts at 0; `next_offset` null ends the page. Pagination only reduces presentation after full parsing/evaluation.

`tick` completes one local scheduling round, running delivery alongside the checks and waiting for the final delivery pass before returning. Inspect each result in `checks` and `delivery`; the outer COMPLETED does not imply every source succeeded. `run` keeps polling and delivery in separate foreground tasks, so slow notifications do not delay subsequent polling rounds. Both operations update heartbeat independently every 30 seconds. `health` reads local heartbeat only and exits 1 if absent/stale.

Errors: NETWORK, RATE_LIMITED, BLOCKED, PARSE, UNSUPPORTED. Requests share a minimum 5-second gap and persistent leases. 429 waits use the later of Retry-After and local backoff. Repeated schema problems pause the target; access refusal pauses the platform. Resume is a manual action and preserves cooldowns.

A complete successful query resets the shared platform failure count without clearing cooldowns or writing monitoring baselines. Cached status summaries cover all items, while detail pages are read with SQL pagination. Historical retention cleanup runs at most once per hour across callers sharing the database; delivery expiry checks remain independent of this cleanup interval.

Outbox: PENDING, INFLIGHT, SENT, CANCELLED, EXPIRED, FAILED, DISABLED. Stable event IDs allow identifying possible delivery duplicates after lost acknowledgements. Without a configured webhook, work remains PENDING until its 10-minute TTL expires.

New target notification payloads include `target_signature`, binding them to the configuration that created them. Each delivery claim has a distinct owner; late responses cannot revive cancelled work or overwrite a newer claim. Already-issued HTTP requests cannot be recalled. Existing pending events without a signature remain readable and use the existing cancellation and current-target checks.

Activity URLs use SESSION and return order_url in full item detail. Order URLs use AREA if the session has ticket areas, otherwise PRODUCT. They accept matching item_ids; activity URLs reject item filters. Items add source_status, availability_text, remaining_count, session_name and order_url. remaining_count is a displayed 0-20 quantity; hot-sale values, including counts above 20, are null. Ticket names retain eligibility distinctions such as disability tickets. Individual seats and purchase success are not queried.

Outer status unavailable is TEMPORARILY_UNAVAILABLE. A prior SOLD_OUT to that status emits RELEASE_HINT, and a later AVAILABLE from either no-ticket status emits RELEASE. First observations and unchanged states emit neither. Inner unavailable is SOLD_OUT with remaining_count 0. Missing or invalid limited inventory is UNKNOWN.

The public session/area/product endpoints were validated on 2026-10-05 and have no official stable inventory contract. MCP is not installed or implemented by this skill.
