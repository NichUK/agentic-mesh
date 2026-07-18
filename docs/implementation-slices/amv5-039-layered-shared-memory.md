# AMV5-039 — Layered shared memory

## Outcome

Provide durable, source-linked memory at project-role, project-wide, and organization-role layers.
Same-role instances see one logical memory while project, role, and organization visibility remain
explicit. Memory accelerates retrieval but never replaces its authoritative source.

## Scope and boundaries

- Store memory by scope, never role-instance or provider-thread id: `project-role` is visible
  to one role in one project, `project` to every role in one project, and `organization-role` to one
  role across projects in one configured organization.
- Require a stable subject, concise summary, tags, actor, and one authoritative source with kind,
  reference, and observed version. Allowed kinds are document, work item, event, decision, policy,
  and release. Threads, turns, prompts, conversations, and model output are not sources.
- Inject source verification. Writes require an existing, current source version.
- Reverify reads and return source state/current version. Current queries exclude stale or removed
  entries; inspection retains them visibly for correction and audit.
- Append immutable history for create, update, retire, and source-status changes. A current pointer
  uses an optimistic version; updates cannot silently overwrite a concurrent role instance.
- Authorization is structural. Project-role, project, and organization-role writes require their
  matching coordinates. Only organization-role memory crosses project boundaries.
- Classification and promotion are deferred to AMV5-040 for sensitivity, redaction, and fail-closed
  promotion policy.
- Provider thread content cannot enter memory or resume conversation; thread affinity remains in its
  dedicated store.

## Acceptance criteria

- Same-role/project instances share one project-role entry; another role or project cannot read it.
- Project entries stay local. Organization-role entries reach only that role in that organization.
- Every entry/revision cites a permitted source and version; missing or stale sources reject writes.
- Changed or removed sources leave current reads and remain visibly stale/removed in inspection,
  and retain history.
- Concurrent updates yield one success and one conflict; exact retries create no extra revision.
- No API or schema accepts thread, turn, conversation, prompt, or role-instance authority.
