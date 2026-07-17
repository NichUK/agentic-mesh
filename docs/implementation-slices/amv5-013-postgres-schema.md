# AMV5-013 — V5 Postgres Schema and Migration Runner

## Outcome

Create the first durable V5 control-plane schema and a small migration runner
that installs and upgrades it without importing the V4 runtime. Postgres is the
only authoritative runtime-state store; containers retain no database fallback.

## Scope

- package ordered, checksum-protected SQL migrations with the V5 Python package;
- apply all pending migrations under one Postgres transaction and advisory lock;
- record the version, name, checksum, and application time in Postgres;
- create the initial project, role, instance, work-item, queue, lease, event,
  outbox, handoff, gate, approval, memory, package, progress, and audit records;
- qualify project-owned foreign keys with `project_id` so a reference cannot
  cross a project boundary accidentally;
- permit explicitly organization-owned package, memory, and audit records while
  requiring `project_id` for their project-owned variants; and
- expose migration and migration-status operations through the V5 bootstrap CLI.

This slice creates storage contracts only. Repository methods, lifecycle logic,
queue claiming, outbox dispatch, and product APIs remain in their ordered later
stories.

## Acceptance criteria

- A clean Postgres database reaches the current schema version automatically.
- Re-running the runner is a no-op, while a checksum change to an applied
  migration fails closed.
- Ordered upgrades are discovered deterministically and duplicate, missing, or
  malformed versions are rejected before database mutation.
- A SQL failure rolls back the migration history and all schema changes made by
  that run; concurrent runners cannot apply the same migration twice.
- Project-owned records carry `project_id`, and composite foreign keys reject
  references to records owned by another project.
- Organization-owned package, memory, and audit records are explicit and cannot
  masquerade as project-owned records without a project identifier.
- Migration status is read from Postgres, contains no secret, and is available
  through the V5 CLI.
- Unit tests, disposable-Postgres integration tests, clean installation, the V5
  runtime boundary check, and the full regression suite pass or retain only
  already documented unrelated baseline failures.
