# Project Import Preview And Activation

AMV5-055 converts one ready AMV5-054 resolution into immutable preview revisions. Each
contains a validated manifest, a decision for every source, and source-linked candidates
that remain inactive until approval and activation.

Preview creation is read-only and excludes V4 operational state. A later revision
supersedes a draft or rejected revision without rewriting it. Approved and activated
revisions cannot be revised.

One configured sponsor may decide the latest revision; approval does not carry forward.
Activation is serialized through an idempotent adapter: the same operation resumes, while
another is rejected. Only the manifest, sponsors, decisions, and selected candidates enter
the request, and the receipt must match them before activation is recorded.

## Acceptance Criteria

- Every preview pins a ready resolution and matching validated V5 manifest.
- Every source is included by exact manifest resource or excluded with rationale.
- Candidates are source-linked and inactive; only selected ids enter activation.
- Draft/rejected previews may be revised; approved/activated revisions cannot.
- Only a resolved project sponsor may decide the exact latest revision.
- Duplicate requests are idempotent; stale or conflicting requests fail closed.
- Activation serializes one operation id; only a matching receipt completes it.
- No V4 operational state exists in the preview or activation contract.
