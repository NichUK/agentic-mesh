# AMV5-049 — ADO delivery primitives

## Outcome

Extend the project-bound AMV5-048 adapter with the two small reliability
primitives required by milestone progress: conditional field updates that
preserve human edits, and comment creation that can reconcile a completed side
effect after worker failure.

## Boundaries

- Keep the active project manifest as the only source of ADO organization,
  project, and credential binding.
- Add optional expected fields to an idempotent update. A mismatch becomes a
  durable terminal conflict and never sends a patch.
- Preserve replay of operations created before expected fields existed.
- Add one stable compact comment marker, scan every continuation page before
  creating a comment, and reject malformed/repeated continuation tokens.
- Do not add milestone sequencing or lifecycle mapping in this slice.

## Acceptance criteria

- A manual state edit is not overwritten and its update operation is visibly
  conflicted rather than left pending.
- Legacy operations retain their original request digest and resume after the
  migration.
- Retry finds a prior marked comment on any response page and creates no second
  comment.
- Project validation, credential isolation, bounded transport retry, and V5
  lifecycle authority remain unchanged.
