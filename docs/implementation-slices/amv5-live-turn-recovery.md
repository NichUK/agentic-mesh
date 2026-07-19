# AMV5 live-turn timeout and pickup recovery

## Trigger

The first real V5 arms-length BA turn exposed two production-only failure
boundaries after worker authentication was corrected:

- a provider thread could remain active indefinitely while lease heartbeats
  continued; and
- stopping that process left `active_operation_id` claimed even after its queue
  lease became reclaimable.

The runtime recorded `provider-turn-timeout` incident
`incident-87412104291fd93467199a4d188386c5` and technical attempt 1 before this
repair began. The work item remains active and unacknowledged.

## Smallest reliable change

1. Add `turn_timeout_seconds` to `RoleServiceConfig` and the `role-service` CLI,
   defaulting to 900 seconds and bounded from 1 to 14,400 seconds.
2. Make the existing heartbeat thread schedule the turn deadline and call the
   provider's existing interrupt contract when it expires. No second scheduler
   or watchdog service is introduced.
3. Before running a claimed item, release an affinity operation only when its
   start time predates the current queue lease acquisition and the new owner is
   an authorized instance of the same role. This supports restart and
   autoscaled cross-instance pickup without allowing an unrelated role or an
   older claim to steal an active thread.
4. If the provider acknowledges `interrupt()` but its event stream does not
   unwind, evict and close the active warm engine. This uses the existing
   provider `close()` contract; the next attempt opens a replacement engine
   while retaining the durable thread id and external Codex home.
5. When a role leases an earlier retry envelope whose exact incident, stage,
   and attempt result is already durable, complete that envelope through its
   current lease without opening a provider turn. Never apply this shortcut to
   the post-recovery resume envelope.

## Acceptance criteria

- A timed-out provider turn is interrupted and reported as
  `provider-turn-timeout`.
- The queue item remains retryable and cannot be marked complete without the
  existing terminal-provider and durable-checkpoint evidence.
- The affinity operation is released when the interrupted call unwinds.
- After an abrupt process stop, a newer queue lease can reclaim an older
  operation from either the same instance or another authorized instance of
  that role.
- A boundary older than the operation, a mismatched prompt digest, an
  unauthorized role, or an operation newer than the queue lease remains
  protected.
- Existing warm-engine, queue, CLI and Codex-provider behavior remains green.
- A provider stream that ignores turn interruption is unblocked by engine
  closure, after which the queue lease and exact affinity operation are
  released and the closed engine cannot be reused.
- Recorded retry results cannot leave stale high-priority envelopes that starve
  the incident's required next attempt; reconciliation is lease-owned,
  idempotent, and does not synthesize a progress checkpoint.

## Verification

- Focused affinity and live-role suite: 38 passed, 1 environment skip.
- Broader queue/CLI/warm-engine/Codex-provider suite: 117 passed, 1 environment
  skip.
- Complete V5 suite: 625 passed, 5 environment skips, with one existing
  Starlette/httpx deprecation warning.
- A second live technical attempt must run from a reviewed immutable image and
  produce a durable checkpoint before this recovery slice is accepted.
- Engine-abort focused verification: 71 passed, 1 environment skip against
  real Postgres. Complete V5 regression verification: 627 passed, 5
  environment skips, with the existing Starlette/httpx deprecation warning.
- Retry-route reconciliation verification: 29 focused real-Postgres tests and
  the complete V5 suite of 629 passed / 5 environment skips, with the same
  existing Starlette/httpx deprecation warning.

## Live attempt-2 finding

The reviewed timeout/reclaim image at merge revision
`250ec5b1d94539c9e872db0e2a8479d795b7c5f5` returned the retry queue item to
`ready` at its 600-second watchdog boundary without acknowledging it. The
Codex provider stream did not finish after `turn.interrupt()`, so the role loop
and `work` affinity remained active until the container was stopped. Technical
attempt 2 is therefore recorded as failed. The bounded engine-abort addition
above is the smallest correction for technical attempt 3.
