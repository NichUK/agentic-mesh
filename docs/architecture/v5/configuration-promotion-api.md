# Configuration Promotion API

AMV5-057 exposes the existing external-package activation model through the
versioned control API. Package content remains edited and versioned in the
external Git repository; an API draft edits the effective composition by
selecting exact package references. This keeps Git canonical and avoids a
second package-file editor in the runtime.

The API process receives that mounted repository through
`AGENTIC_MESH_V5_CONFIG_ROOT`. Project authorization still applies to every
operation even when projects share one organization repository.

A project draft records its package references and expected active digest.
Validation resolves those references deterministically, captures the complete
resolved snapshot, and produces a structural diff against the active release.
Validation failure creates no release and changes no active pointer.

After validation, a draft authored by a current project sponsor is implicitly
approved. Every other draft requires an explicit approve/reject decision from
a current sponsor. Only an approved, unchanged validation may create its
digest-addressed immutable release and atomically activate it. Rollback may
target only an existing release and requires a current sponsor plus a reason.

## Acceptance Criteria

- Project-authorized APIs create, inspect, validate, diff, decide, activate, and roll back drafts.
- Validation pins exact references, active baseline, resolved digest, and structural diff.
- Sponsor-authored validations are implicitly approved; all others require sponsor approval.
- Rejected, stale, unvalidated, or foreign-project drafts cannot activate.
- Activation uses optimistic expected-active checks and is idempotent on replay.
- Digest-addressed releases remain immutable and activation history remains auditable.
