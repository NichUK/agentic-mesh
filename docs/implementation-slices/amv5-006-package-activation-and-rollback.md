# AMV5-006 — Package Activation And Rollback

## Outcome

Promote a validated effective configuration into an immutable release, switch
one Git-visible activation pointer atomically, and roll back to an earlier
release without changing package content.

## Scope

- validate drafts through the AMV5-005 deterministic resolver;
- create digest-addressed immutable release records under the external
  configuration repository;
- serialize activation with a small cross-process repository lock;
- commit the active digest and its audit event in one atomically replaced JSON
  document;
- support optimistic expected-current checks for concurrent operators; and
- roll back by pointing to an existing immutable release.

This bootstrap slice does not add a database, deployment orchestrator, Git
commit automation, approval workflow, or secret resolution. Postgres arrives
in AMV5-013; configuration approval and promotion APIs arrive in AMV5-057.
The files remain visible so normal Git review can own configuration history.

## Acceptance criteria

- Invalid drafts create no release and do not alter the active pointer.
- A release is addressed by the effective configuration digest, contains its
  complete resolved output, and cannot be overwritten with different content.
- Activation replaces one document containing both the pointer and audit
  history, so a failed replace preserves the previous pointer and history.
- Concurrent activations using the same expected-current digest allow exactly
  one winner; the other fails without overwriting it.
- Rollback targets an existing release, records actor, reason, previous digest,
  target digest, timestamp, action, and monotonic revision.
- Repeating activation of the already-active digest is an idempotent no-op.
- CLI and automated tests cover validation, release immutability, activation,
  concurrency, failure immediately before commit, idempotency, and rollback.
