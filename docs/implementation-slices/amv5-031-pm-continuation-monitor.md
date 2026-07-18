# AMV5-031 — Global PM continuation monitor

## Outcome

One durable logical Project Manager monitor sweeps every active project. Its
process owner may restart, but a singleton lease prevents concurrent monitors
and preserves the logical identity `global-project-manager`.

## Decisions

- Reuse Postgres work items, queues, leases, gates, routing and audit records.
- A ready/delayed queue item or an item with a live lease is a durable
  continuation. Expired leases are not.
- A pending sponsor gate is an intentional stop, not an orphan.
- Active work with structured `material_ambiguity: true` and a non-empty
  `sponsor_question` opens one deterministic sponsor-clarification gate.
- Nonterminal work with no continuation is routed once to that project's
  `project-manager` queue for its current version.
- Missing PM routing is persisted and returned as `routing_blocked`; it can
  never disappear as an apparently healthy sweep.
- Current disposition is stored per work item. Material changes also append to
  the existing project audit log.

## Acceptance criteria

1. Only one unexpired monitor owner can sweep; a new process can take over after
   expiry without changing the logical PM identity.
2. One sweep covers all active projects and does not share project context.
3. Gated work with pending approval remains `waiting_sponsor` and is not routed.
4. Structured material ambiguity opens one sponsor question and repeated
   sweeps create neither duplicate gates nor approvals.
5. Orphaned new or active work creates one idempotent PM continuation route;
   repeated sweeps and monitor restart create no duplicate queue item.
6. Ready/delayed queue work and work held by an unexpired lease are progressing;
   an expired lease alone is not a continuation.
7. Missing project-manager role/queue is a visible `routing_blocked` result and
   audit record.
8. Operator-authenticated API operations claim, heartbeat, sweep and inspect
   the global monitor without exposing the lease token in status/read models.

## Test plan

- Race monitor claims and simulate owner expiry/takeover.
- Sweep multiple projects containing orphaned, progressing, ambiguous and gated
  work, including multiple simultaneous sponsor gates.
- Repeat sweeps across a PM restart and reconcile queue/gate counts.
- Exercise missing PM routing, expired work leases, API authorization and
  migration upgrade paths.
- Run focused, full V5, known V4 baseline, package/secret and clean-clone checks.
