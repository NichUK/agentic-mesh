# Project Import Preview And Activation

AMV5-055 turns one ready AMV5-054 resolution into immutable preview revisions containing
a validated manifest, a decision for every source, and inactive source-linked candidates.
Preview creation is read-only and excludes V4 operational state. A later revision may
supersede a draft or rejection, but approved and activated revisions are final.
One configured sponsor decides the latest revision; approval does not carry forward.
Activation uses one serialized, idempotent operation: the same operation resumes and any
other is rejected. Its receipt must match the manifest and selections before completion.

## Acceptance Criteria

- Every preview pins a ready resolution and matching validated V5 manifest.
- Every source is included by exact manifest resource or excluded with rationale.
- Candidates are source-linked and inactive; only selected ids enter activation.
- Draft/rejected previews may be revised; approved/activated revisions are final.
- Only a resolved project sponsor may decide the exact latest revision.
- Duplicate requests are idempotent; stale or conflicting requests fail closed.
- Activation serializes one operation id; only a matching receipt completes it.
- No V4 operational state exists in the preview or activation contract.
