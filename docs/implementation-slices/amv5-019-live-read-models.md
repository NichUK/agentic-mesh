# AMV5-019 — Live Read Models and SSE

## Outcome

Provide rebuildable, query-oriented project state and an ordered live update
feed that authorized clients can resume without WebSockets.

## Scope

- append safe projection events transactionally for project, work, queue, role,
  role-instance, and structured-progress changes;
- backfill projection events for rows that predate this migration;
- replay events into project/domain/entity read models without mutating source
  tables or the authoritative lifecycle event journal;
- expose an authorized project snapshot under `/api/v1`;
- stream project-filtered SSE with global event IDs and `Last-Event-ID` resume;
- fetch bounded batches without holding a database connection while yielding to
  slow clients; and
- emit only safe progress fields, never private reasoning or role metadata.

Dashboard-specific aggregations, traffic-light rules, event retention, and
distributed projection workers remain in their ordered later stories.

## Acceptance criteria

- Clearing and replaying one project's projection produces the same current
  snapshot without changing any authoritative source row.
- Committed source mutations have a same-transaction projection event; rolled
  back mutations have none.
- Update IDs are strictly increasing and reconnect resumes after the supplied
  ID without losing later committed project events.
- A project's snapshot and stream never include another project's entities or
  events, including when global IDs are interleaved.
- A slow or disconnected stream holds no database transaction and can resume
  from its last delivered ID.
- Invalid cursors fail with a structured response, and authorized clients need
  no WebSocket support.
- Real Postgres replay/rollback/order/isolation tests, API/SSE contracts, V5
  boundary, packaging, and regression checks pass.
