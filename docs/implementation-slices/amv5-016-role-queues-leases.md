# AMV5-016 — Role Queues and Durable Leases

## Outcome

Provide project-aware role queues whose ready work can be claimed by one role
instance at a time, renewed with a lease token, and reclaimed automatically
after expiry.

## Scope

- create one project/role-qualified queue and idempotency-qualified items;
- order eligible work by priority, ready time, and insertion order;
- claim with `FOR UPDATE SKIP LOCKED` and a database-generated lease record;
- authenticate heartbeat, completion, and release with the exact lease token;
- use Postgres time for readiness, heartbeat, expiry, and reclaim;
- release expired leases and return their items to `ready` inside claim;
- reject stale, released, expired, and foreign-project lease operations; and
- expose total, ready, delayed, leased, oldest-ready-age, and attempts metrics.

Autoscaling, retry caps, routing policy, and dead-letter behavior remain in their
ordered later stories.

## Acceptance criteria

- One queue item has at most one active valid lease under concurrent claimers.
- An expired item is reclaimed and claimed by another valid role instance
  without manual repair; the old token can no longer mutate it.
- Priority and ready time determine claim order, while future work remains
  delayed until database time makes it eligible.
- Heartbeat extends an active lease and completion/release are each applied once.
- A project cannot claim, heartbeat, complete, release, or inspect another
  project's queue or lease.
- Queue depth, ready/delayed/leased counts, oldest ready age, and accumulated
  attempts are queryable without scanning container state.
- Unit tests, real Postgres concurrency/expiry/isolation tests, V5 boundary and
  packaging checks, and the full regression suite pass or retain only the
  documented unrelated baseline failures.
