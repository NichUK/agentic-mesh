# AMV5-033 — Retry and terminal-error policy

## Outcome

Prevent an active work item from entering terminal error until one durable
failure incident has exhausted, in order:

1. three automatic technical retries;
2. three Project Manager corrective attempts with distinct instructions; and
3. one independent recovery request.

An attempt success ends the incident without ending the work item. A successful
recovery also routes the work back to its configured owner. Only a failed
recovery makes terminal error eligible, and the lifecycle transition consumes
that eligibility atomically.

## Boundaries

- Reuse project role queues and the existing idempotent router for technical and
  PM continuations.
- Store recovery as a durable request for the independent supervisor delivered
  by AMV5-034; do not represent it as a normal role queue.
- Store safe failure summaries and source references, never provider exceptions
  or credentials.
- Keep completed attempt rows append-only. Database triggers reject update and
  delete, while idempotency keys make caller retries safe.
- Keep policy fixed at three technical, three PM and one recovery attempt for
  this story. Configuration belongs only in a later story if operational
  evidence shows it is needed.
- Do not add a scheduler, broker, alternate work-item lifecycle, or new worker
  orchestration path.

## Acceptance criteria

- Starting an incident creates technical retry 1 as a durable role-queue item.
- Failed technical attempts 1 and 2 route the next technical retry; failure 3
  routes PM correction 1.
- Failed PM attempts 1 and 2 route the next correction; failure 3 creates one
  durable recovery request.
- PM correction instructions are non-empty and unique within the incident.
- A failed recovery makes terminal error eligible. Direct or early terminal
  error attempts fail without changing work, incidents, events, or outbox.
- The terminal lifecycle transaction marks the incident terminal and records
  its identifier in terminal evidence.
- A success at every stage ends the incident as recovered. Recovery success
  creates exactly one owner continuation.
- Repeated and concurrent operation IDs produce one incident, attempt, route,
  recovery request, and terminal transition.
- Attempt history cannot be updated or deleted.
- PM continuation monitoring treats a pending recovery request as durable work.

## Test plan

- Exercise success and failure at every stage and verify the exact 3/3/1 order.
- Try duplicate and out-of-order attempts, repeated PM instructions, early
  terminal transitions, and concurrent incident starts.
- restart stores between stages and verify the next action is reconstructed
  entirely from Postgres;
- mutate attempt rows directly and verify database rejection;
- inject a missing role route and verify the state/attempt transaction rolls
  back;
- verify recovery success resumes the owner exactly once;
- verify API authorization, OpenAPI contracts, migration installation, audit
  records, and redacted public errors.
