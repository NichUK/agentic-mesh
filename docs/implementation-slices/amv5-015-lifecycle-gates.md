# AMV5-015 — Projects, Work Items, Gates, and Approvals

## Outcome

Provide the durable project/work-item lifecycle used by the V5 kernel, including
sponsor identity, ownership-preserving gates, approval evidence, optimistic
concurrency, and distinct terminal completion and error outcomes.

## Scope

- register a project with one or more authorized sponsor identities;
- create project-owned work items with an owning role and version;
- apply the small kernel lifecycle `new → active → completed|error`;
- pause active work through `active → gated` only by opening a sponsor gate;
- resume gated work only through one authorized approve/reject decision;
- preserve the work item's owner while a gate is pending and after it resolves;
- retain requester, sponsor, rationale, evidence, correlation, and timestamps;
- reject stale versions, duplicate decisions, invalid transitions, unknown
  identities, and cross-project references; and
- record every accepted work-item and gate change through the AMV5-014
  journal/outbox transaction.

The later external flow engine owns detailed SDLC states and conditional routes.
This slice supplies only the durable lifecycle and gate invariants it needs.

## Acceptance criteria

- Only defined kernel transitions succeed, with gated resumption restricted to
  the approval path and terminal work immutable.
- A pending gate leaves the owning role unchanged and one concurrent sponsor
  decision resolves it exactly once.
- Unauthorized, duplicate, stale, and cross-project decisions mutate nothing.
- Approval identity, decision, rationale, evidence, and time remain queryable.
- `completed` and `error` are distinct terminal states with retained evidence.
- Lifecycle state, journal event, and outbound notification commit or roll back
  together.
- Unit tests, disposable-Postgres concurrency/isolation tests, V5 boundary and
  packaging checks, and the full regression suite pass or retain only the
  documented unrelated baseline failures.
