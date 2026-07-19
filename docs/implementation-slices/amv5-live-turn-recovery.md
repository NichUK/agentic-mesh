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
6. Treat each project-scoped worker container as the tooling isolation boundary
   and run Codex with its inner sandbox set to full access. The nested Linux
   `bubblewrap` namespace cannot start inside the deployed worker container and
   otherwise prevents the agent from calling the durable control API. A
   deployment may explicitly select a stricter supported policy through
   `AGENTIC_MESH_V5_CODEX_SANDBOX`; the value is validated and pinned when the
   role service starts.
7. Do not query `thread/read` through the app-server connection that owns the
   active turn. That synchronous request can block behind the turn and prevent
   later terminal reconciliation. Continue consuming only the owner's SDK
   notification queue so output, errors and usage remain captured.
8. Wait for the configured queue poll interval after any released item so a
   repeatable provider or configuration failure cannot create a tight claim and
   release loop.
9. After the owning stream goes silent, inspect only the bounded tail of that
   thread's local rollout for an exact `task_complete`/`turn_id` hint. Only
   after that terminal hint may a fresh observer app-server verify the exact
   turn through public `thread/read`; an observer must never resume an active
   turn.
10. Treat an `error` event as terminal only when its provider classification is
    non-retryable or missing. When the provider explicitly marks the error as
    retryable, continue the same event stream so its internal retry can reach a
    terminal event without releasing the lease or starting a second turn.

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
- A role can execute the V5 control CLI inside its project-scoped worker
  container without requiring host namespace privileges.
- A terminal turn verified by the fresh observer completes the provider
  iterator even if its terminal stream notification is absent; the owner
  connection is never queried and exact thread and turn ids are checked before
  reconciliation.
- Released work is rate-limited by the configured poll interval while completed
  work may continue immediately to the next queue item.
- The independent observer is launched only after an exact terminal rollout
  hint, cannot interrupt active work, and must still verify the exact public
  thread/turn terminal state before completion is synthesized.
- An explicitly retryable provider error cannot release the queue item; a
  subsequent attributable checkpoint and completed turn close the same lease.
  Non-retryable or malformed errors still fail closed.

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

## Live attempt-3 preflight findings

- A fleet recreation command mounted role volumes using a one-character-short
  container-name suffix. The valid external volumes and their persisted Codex
  threads were never deleted. All 16 role containers now mount the exact
  `amv5-codex-<role>-<instance>` volume; the mismatch count is zero.
- With the correct BA volume restored, the full pinned prompt successfully
  resumed the existing `work` thread. The retry's Codex task completed in
  approximately 12 seconds, but the SDK iterator remained blocked even though
  `thread/read` reported the exact turn as `completed`.
- The completed agent response reported that nested `bwrap` could not create a
  namespace in the worker container, so `agentic_mesh_v5 control-call` could not
  record a checkpoint. No progress record or formal technical-attempt-3 outcome
  was created. The BA was stopped before deployment of this repair.
- Focused provider and real-Postgres role-service verification for the repair is
  34 passed after the reviewed sandbox override. The final complete V5 suite is
  635 passed / 5 environment skips, with
  the existing Starlette/httpx deprecation warning. A repository-wide run also
  passed every V5 test but retained 11 unrelated V4 baseline failures.

## Live independent-observer finding

- Revision `88e892d145552b2f210ada0ed6794e31e19850c0` deployed cleanly to the
  control plane and all 16 role containers with zero revision or Codex-volume
  mismatches. The PM remained healthy and autoscaling started the Research
  Analyst when BA routed its consultation.
- BA and Research Analyst recorded durable progress sequences 1 and 2. Their
  exact Codex rollout turns then recorded `task_complete`, but the owning
  app-server connections continued to report active leases. Fresh app-server
  connections verified both exact turns as `completed`.
- A diagnostic proved that a second app-server which resumes the thread before
  `task_complete` interrupts the active turn. The observer correction therefore
  uses the exact bounded rollout event only as a terminal hint and still relies
  on fresh public `thread/read` as the authoritative completion result.
- BA and Research Analyst were stopped before watchdog expiry. Their durable
  checkpoints remain; technical attempt 3 has no fabricated outcome, and the
  flow consultation obligation remains pending until normal pickup resumes.
- The independent-observer slice passed 36 focused provider/live-role tests.
  The complete V5 suite passed in six deterministic batches: 637 passed and 5
  environment skips, with only the existing Starlette/httpx deprecation
  warning. The single aggregate invocation exceeded its ten-minute command
  ceiling; every constituent file completed successfully in the bounded
  batches.

## Live rollout-first finding

- Revision `24b3d5e2b2fa7498f9b414cf4e3b0ba079126885` deployed cleanly to the
  control plane and all 16 role containers. Direct/TLS health, image revision
  labels, and all exact full-name Codex volume bindings were verified.
- Retried BA and Research Analyst turns both recorded exact `task_complete`
  events. Research Analyst added progress sequence 3, confirming the prior
  response remained durably and idempotently routed, but both queue leases
  remained open.
- Inside both live containers the rollout hint returned true and a separately
  opened public `thread/read` returned the exact turn as `completed`. The
  remaining blocker is therefore the owner's earlier synchronous
  `thread/read`, which can block before the terminal hint exists and prevent
  the event loop from ever checking it again.
- Branch `codex/v5-rollout-first-terminal-reconciliation` removes owner reads
  from reconciliation. Only an exact local terminal hint can authorize the
  fresh observer. BA and Research Analyst were stopped before watchdog expiry;
  no technical-attempt-3 outcome was fabricated.
- The rollout-first correction passed 36 focused provider/live-role tests and
  the complete V5 suite in bounded groups: 637 passed and 5 environment skips,
  with only the existing Starlette/httpx deprecation warning.

## Live PM-correction finding

- The rollout-first release merged as
  `57988d574bf16a9a9b80db8c79a0aba794fbf926` and deployed cleanly across
  control and all 16 role containers. Direct/TLS health, PM health, revision
  labels, and exact Codex volume bindings passed.
- The reviewed technical-3 BA lease reached its 600-second watchdog and was
  released without queue completion. Technical attempt 3 is now durably
  recorded as failed with the exact revision, lease, completed rollout turn,
  blocked provider-handle turn, checkpoint and clean-stop evidence. Reliability
  correctly advanced to PM correction attempt 1.
- The PM recorded progress sequence 7 and routed one distinct minimal BA
  correction. A supported `role-service --once` diagnostic first reconciled the
  already-recorded technical envelope, then processed the correction envelope
  and returned the safe result `provider-not-completed`.
- Lease timing and exact rollout ids prove that the first provider turn was
  released about three seconds after `turn/started`, continued as a ghost and
  later completed; the immediate reclaim opened a different handle that never
  started. The role service was treating an explicitly retryable provider
  `error` event as terminal while Codex continued its internal retry.
- Branch `codex/v5-retryable-provider-events` continues only explicitly
  retryable errors and retains fail-closed handling for all other errors. PM,
  BA and Research Analyst are stopped while the correction is reviewed; the
  correction envelope remains durable and retryable.
- The PM-correction slice passed 38 focused provider/live-role tests. The
  complete V5 suite passed 639 tests with 5 environment skips and only the
  existing Starlette/httpx deprecation warning.

## Live cumulative-usage finding

- Rollout-authoritative terminal reconciliation now holds the queue lease until
  the exact rollout hint and fresh terminal read, but the role service releases
  the lease immediately afterwards with the safe `turn-failed` result.
- A content-free exception trace identifies `UsageConflict` while recording the
  final `thread/tokenUsage/updated` notification. Codex reports `last` usage for
  each model invocation inside one outer turn; those individual counters may
  decrease even though the turn's cumulative usage only increases.
- Acceptance for the correction is: the Codex provider exposes cumulative
  usage for one product turn, including multiple model invocations; each
  emitted cumulative observation is monotonic and retains the provider's exact
  token totals; an identical durable observation remains idempotent and a
  lower or differently owned durable observation remains a conflict.
- Usage normalization must not suppress the exact terminal event, change
  delivery or handoff semantics, parse prompt/response content, or weaken the
  existing durable conflict checks. The PM and BA remain stopped until the
  correction is reviewed and deployed.
- The correction derives each outer-turn observation from the provider's
  monotonic thread-total snapshot minus the pre-turn baseline. This preserves
  exact multi-invocation totals and makes a replayed snapshot idempotent.
  Verification is 70 focused provider/affinity/live-role tests with one
  environment-only skip and 97 passed tests in the affected V5 batch.

## Live qualification result

- PR 451 merged as `d93791ef0cacf6b39208a274589d2206a19ae480` and was
  built into seven digest-pinned images. Direct and TLS health, exact revision
  labels, commands, environments, mounts and all 16 full-name Codex volumes
  match the retained `5de1264e` rollback fleet.
- PM correction `route-b3cda59958669bcf76a778d59c183599` completed on
  attempt 23. BA correction `route-851759e1ddfaad93e6825bc1dbf17cf0`
  completed on attempt 2. The historical BA source, consultation return and
  Research Analyst source then completed normally on attempts 167, 1 and 7.
  No ready or leased work remains for this work item.
- Usage records for the successful PM and BA turns contain cumulative
  multi-invocation totals and no longer raise `UsageConflict`. Every repaired
  route reached durable queue completion after its attributable checkpoint.
- Reliability incident `incident-87412104291fd93467199a4d188386c5` is
  durably `recovered`: technical attempts 1 through 3 remain failed and PM
  correction attempt 1 is recorded succeeded with exact revision, image,
  test, health, route and progress evidence.
- The work correctly remains in `business_analysis`. Its artifact,
  consultation and owner-review obligations are pending because the existing
  brief identifies seven material sponsor questions. No product handoff,
  lifecycle advancement or sponsor decision was fabricated.
- After their queues emptied, the Business Analyst and Research Analyst
  instances automatically reached `hibernated`; the Project Manager remains
  healthy as the warm project leader. The work item has eight completed queue
  records and none ready or leased.
