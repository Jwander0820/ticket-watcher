# Tool contract v1.0

Every JSON result includes `schema_version: "1.0"`, `execution_status` and `result_source`.

Execution status: COMPLETED (exit 0), FAILED (2), DEFERRED (3), UNSUPPORTED (4). Source: LIVE, CACHE, LOCAL. A completed partial observation still has `complete: false` and UNKNOWN items.

`query --url <URL>` returns current availability, `evaluation.performed: false`, `release_detected: null`, and `notification.status: NOT_APPLICABLE`. It never writes monitoring baselines or creates events. Shared request controls and metadata cache are persisted.

`check --target <id>` respects the persisted schedule, compares all returned items, saves changes, and immediately attempts configured notification delivery. `changes`, `changes_total`, `changes_next_offset`, `event_id`, `next_allowed_at` and `notification` describe confirmed-release work. `hint_event_id`, `hint_notification` and `evaluation.release_hint_detected` separately describe outer-page release hints. A hint does not set release_detected true. Both kinds use the same TTL, delivery controls and cancellation rules.

Change timestamps `previous_observed_at` and `observed_at` are UTC Unix seconds in event payloads. Top-level times and cached item times are ISO 8601 UTC. `monitoring_gap` is true when valid observations are over 45 minutes apart. Event detection time is not an exact inventory update time.

`status --target <id> --detail full` reports last valid state/time separately from current observation. `events` returns compact summaries of state changes, errors, system alerts and releases with delivery status; add `--detail full` for original change payloads. `--limit` is 1–500; `--offset` starts at 0; `next_offset` null ends the page. Pagination only reduces presentation after full parsing/evaluation.

`tick` completes one local scheduling round. Inspect each result in `checks` and `delivery`; the outer COMPLETED does not imply every source succeeded. `run` repeats tick in the foreground. `health` reads local heartbeat only and exits 1 if absent/stale.

Errors: NETWORK, RATE_LIMITED, BLOCKED, PARSE, UNSUPPORTED. Requests share a minimum 5-second gap and persistent leases. 429 waits use the later of Retry-After and local backoff. Repeated schema problems pause the target; access refusal pauses the platform. Resume is a manual action and preserves cooldowns.

Outbox: PENDING, INFLIGHT, SENT, CANCELLED, EXPIRED, FAILED, DISABLED. Stable event IDs allow identifying possible delivery duplicates after lost acknowledgements. Without a configured webhook, work remains PENDING until its 10-minute TTL expires.

Activity URLs use SESSION and return order_url in full item detail. Order URLs use AREA if the session has ticket areas, otherwise PRODUCT. They accept matching item_ids; activity URLs reject item filters. Items add source_status, availability_text, remaining_count, session_name and order_url. remaining_count is a displayed 0-20 quantity; hot-sale values, including counts above 20, are null. Ticket names retain eligibility distinctions such as disability tickets. Individual seats and purchase success are not queried.

Outer status unavailable is TEMPORARILY_UNAVAILABLE. A prior SOLD_OUT to that status emits RELEASE_HINT, and a later AVAILABLE from either no-ticket status emits RELEASE. First observations and unchanged states emit neither. Inner unavailable is SOLD_OUT with remaining_count 0. Missing or invalid limited inventory is UNKNOWN.

The public session/area/product endpoints were validated on 2026-10-05 and have no official stable inventory contract. MCP is not installed or implemented by this skill.
