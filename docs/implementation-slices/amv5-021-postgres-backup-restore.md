# AMV5-021 — Postgres Backup, Restore, and Outage Behaviour

## Outcome

Create consistent, verifiable V5 Postgres backups; restore them only into an
empty target; and fail safely when the authoritative store is unavailable.

## Scope

- add one durable global maintenance state and append-only transition history;
- enter maintenance by waiting for active table writers, then reject all new
  authoritative mutations at the database boundary while reads continue;
- resume explicitly and make repeated pause/resume commands idempotent;
- create atomic native Postgres custom-format archives with a checksum and a
  secret-free manifest of schema version and authoritative table counts; an
  exclusive output reservation prevents concurrent publishers, and the
  manifest is the publication marker;
- reject a `pg_dump` client older than the server or a mismatched `pg_restore`
  before entering maintenance;
- verify archives with `pg_restore --list`, always resume a pause initiated by
  a completed/failed backup command, and leave a pre-existing pause untouched;
- restore only a checksum-valid archive into an empty database, validate all
  recorded table counts and migration state, and leave the restored runtime
  paused for an explicit operator resume; and
- expose schedulable CLI commands using the existing externally supplied
  database URL. Passwords are passed to native tools only through process
  environment, never command arguments, output, manifests, or errors. Supplied
  connection options must map exactly to native-tool environment variables or
  the operation fails closed rather than weakening the connection contract.

Cloud snapshot policy, retention scheduling, encryption/key management, and
cross-region replication remain deployment concerns behind this local-first
contract.

## Acceptance criteria

- Entering maintenance waits for an already-running write transaction to
  finish, after which direct SQL and API mutations fail while reads work.
- A backup is published atomically only after native archive verification and
  contains every transaction acknowledged before maintenance completed.
- A failed backup restores the prior maintenance state and leaves no published
  archive or manifest.
- Restore rejects missing/tampered manifests, non-empty targets, pending or
  invalid migration state, and table-count mismatches with redacted errors.
- A clean-database restore reproduces projects, work, events, outbox, queue,
  lease, approval, progress, audit, and projection state without duplicates.
- Restored state remains maintenance-paused until an explicit resume succeeds.
- Database interruption rolls back an unacknowledged transaction; API health
  and mutations report a redacted unavailable state and recover after return.
- Real Postgres pause/race, backup, tamper, restore, interruption, migration,
  CLI, boundary, packaging, and regression checks pass.
