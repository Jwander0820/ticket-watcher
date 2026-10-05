---
name: ticket-watcher
description: Use an installed Ticket Watcher CLI to query public TicketPlus session availability, read cached monitoring state, or run explicitly authorized release checks without browser automation.
---

# Ticket Watcher

1. Run `ticket-watcher capabilities --json` to check the installed version and supported granularity.
2. Use the monitoring process's config and SQLite database. With Docker, execute the CLI inside the running container.
3. Use `status` and `events` for saved results, `query --url` for one-off lookup, and `check --target` or `tick` only for authorized monitoring work.
4. Query does not detect release transitions. Only the core's RELEASE event confirms an observed SOLD_OUT → AVAILABLE transition.
5. State the original observation time for CACHE results; UNKNOWN is not a confirmed current state. Respect DEFERRED, platform pauses and cooldowns.
6. PENDING is queued; SENT with message_id is confirmed delivery. Do not send another notification when the tool owns delivery.
7. The first release supports session granularity. Do not silently drop requested area/item filters or claim seat inventory.
8. Default to compact summaries; use `--detail full --limit 50 --offset 0` when necessary. Do not replace the tool with browser clicks, screenshots or another crawler.

Read [references/tool-contract.md](references/tool-contract.md) for fields, errors and CLI examples.
