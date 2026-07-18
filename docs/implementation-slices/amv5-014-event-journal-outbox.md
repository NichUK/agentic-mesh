# AMV5-014 — Event Journal and Transactional Outbox

## Outcome

Record significant V5 transitions as ordered, append-only project events and
create their outbound delivery records in the same Postgres transaction as the
source change. A committed change cannot lose its event or outbox message, and
an uncommitted change publishes nothing.

## Scope

- add explicit work-item, actor, correlation, and optional causation identifiers
  to event records;
- expose one small unit-of-work boundary for source SQL, event append, and one
  or more outbound messages;
- create deterministic, unique idempotency keys for each event/topic delivery;
- read the project journal in committed event order;
- reject migration from unsupported, manually populated v1 journals and retain
  projects with journal history rather than cascading event deletion;
- dispatch one pending outbox message under `FOR UPDATE SKIP LOCKED`, recording
  success or a bounded failure summary and database-timed exponential backoff;
  and
- deliver the same idempotency key again after a crash between the external
  side effect and the database commit, allowing the receiver to deduplicate.

Delivery is intentionally at least once. Exactly-once behavior across Postgres
and an external system is not claimed; downstream adapters must honour the
stable idempotency key.

## Acceptance criteria

- Source mutation, event, and all outbound records commit or roll back together.
- A committed event contains project, work-item, actor, correlation, aggregate,
  type, payload, and ordered event identifiers.
- Project journal reads cannot return another project's events and preserve
  committed event order.
- Concurrent dispatchers cannot deliver the same pending row concurrently.
- A normal retry does not redeliver an already completed row.
- A crash after the external call but before commit leaves the row pending and
  the next attempt uses the identical idempotency key.
- A delivery exception records an attempt and redacted error without marking
  the row dispatched; retry backoff is capped at 256 seconds so a poison row
  cannot monopolize the dispatcher.
- The v1-to-v2 migration fails explicitly if pre-writer event rows exist, and a
  project with journal history must be retired rather than hard-deleted.
- Migration upgrade, unit tests, disposable-Postgres integration tests, V5
  boundary checks, packaging, and the full regression suite pass or retain only
  the documented unrelated baseline failures.
