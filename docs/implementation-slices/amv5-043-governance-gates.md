# AMV5-043 — TOGAF alignment, RACI, and owner gates

## Outcome

Add durable governance evidence to the external flow so required consultations, architecture
classification and conformance, document-owner review, and justified exceptions cannot be silently
bypassed.

## Boundary

- Flow definitions continue to declare artifacts, consultations, informs, gates, owners, and
  conditional routes. Governance records decisions and evidence; it does not route lifecycle state.
- Document evidence is accepted only after the configured project DocumentStore confirms the
  artifact path and immutable version/etag. Records retain the path and version for portal viewing.
- A gate decision rechecks that the reviewed document is still at the verified etag and records that
  path/version with the decision, so a post-verification edit cannot receive a stale approval.
- Required consultations must have a response from the configured role or an explicit exception
  with a non-empty reason before transition readiness. Inform actions require durable dispatch only.
- Owner, peer, architecture-impact, and conformance gates record approved, rejected, or justified
  exception decisions. Sponsor gates remain reserved for AMV5-044.
- Architecture-impact classification is stored as `no-material`, `material`, or `uncertain` and is
  injected into route selection. A caller cannot supply a conflicting value to bypass enterprise
  alignment or conformance.
- Rejection remains visible and does not satisfy a gate. Exceptions are append-only evidence and
  never erase the original obligation.
- Do not add another workflow engine, document copy, architecture model, or approval mechanism.

## Acceptance criteria

- No-material, material, and uncertain classifications select the configured path, and material or
  uncertain work cannot bypass enterprise alignment.
- Required RACI consultation responses or justified exceptions are durable and required for
  transition readiness.
- Missing documents and rejected owner/conformance reviews leave their obligations unsatisfied.
- Accepted documents retain a DocumentStore path and etag that can be opened by later portal/D8A
  integrations.
- Governance records are immutable, project scoped, attributable, and inspectable.
