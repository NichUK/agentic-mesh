# AMV5-029 — Idempotent Routing and Delivery

## Outcome

V5 accepts one project-qualified routing request, resolves one role/capability
queue, and creates at most one queue item for its idempotency key. Outbox
delivery is restricted to adapters that accept and deduplicate the stable key.

## Boundaries

- Routing uses the existing Postgres role queues; no second broker or rules
  engine is introduced.
- A route names project, work item, target role, optional capability, priority,
  payload, availability time, and a mandatory idempotency key.
- Each project role has at most one queue for a capability. Null capability is
  the role's general queue and is distinct from a specialist capability.
- Exact route retries return the original item. Reusing a key with different
  work, target, priority, timing, or payload fails closed.
- Outbox retries always present the same key to a typed idempotent adapter. The
  adapter owns external-side deduplication, which is the only reliable way to
  prevent a second external action after a process crash.
- Project, role, capability, queue and work-item validation happens before a
  new route is committed. Provider and connector choices remain adapters.

## Acceptance criteria

1. Role/capability/priority routes resolve deterministically inside the named
   project and claims retain priority order.
2. Concurrent duplicate route requests create one queue item and return it to
   every caller; conflicting reuse is rejected.
3. Missing, paused, foreign-project, or capability-mismatched targets fail
   without creating queue work.
4. Delivery rejects an untyped callable or mismatched receipt. A simulated
   crash after the external action retries the same key and the adapter performs
   the action only once.
5. The authenticated control API exposes routing without private database
   access and preserves existing direct queue operations for bootstrap use.

## Test plan

- Run Postgres routing tests for role/capability selection, priority, exact and
  conflicting duplicates, concurrency, invalid targets, and project isolation.
- Run outbox tests through an idempotent fake adapter, including failure,
  backoff, concurrent dispatch, project filters, and crash-after-action replay.
- Run API authorization/isolation coverage, all V5 tests, the unchanged V4
  baseline, package/secret checks, and clean-clone acceptance.
