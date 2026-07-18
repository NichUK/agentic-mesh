# AMV5-039 — Layered shared memory

## Outcome

Provide durable, source-linked memory at project-role, project-wide, and
organization-role layers. Same-role instances see one logical memory while
project, role, and organization visibility remain explicit. Memory accelerates
retrieval but never replaces or silently outranks its authoritative source.

## Scope and boundaries

- Store memory by logical scope, never role-instance or provider-thread id:
  `project-role` is visible to one role in one project, `project` to every role
  in one project, and `organization-role` to one role across projects in one
  configured organization.
- Require a stable subject key, concise summary, tags, actor, and exactly one
  authoritative source containing kind, reference, and observed version.
  Allowed source kinds are document, work item, event, decision, policy, and
  release. Provider threads, turns, prompts, conversations, and model output are
  not authoritative memory sources.
- Use an injected source verifier so local tests and future document/work/event
  adapters share one contract. A write is accepted only when the cited source
  exists and its observed version is current.
- Reverify sources while reading. Return source state and current version with
  every entry. Current-memory queries exclude stale or removed entries;
  inspection queries retain them visibly for correction and audit.
- Keep immutable revision history for every create, update, retire, and source
  status change. A current pointer carries an optimistic integer version;
  updates require the expected version and cannot silently overwrite a
  concurrent role instance.
- Scope authorization is structural: project-role writes require the matching
  project and role, project writes require the matching project, and
  organization-role writes require the matching organization and role.
  Cross-project visibility is available only through organization-role memory.
- Do not classify or promote content in this slice. AMV5-040 adds sensitivity,
  project-specific classification, redaction, and fail-closed promotion policy.
- Do not place provider thread content in memory or use memory to resume a
  conversation. Work-item thread affinity remains solely in the thread store.

## Acceptance criteria

- Two instances of the same role/project read and update the same project-role
  entry, while another role or project cannot read it.
- Project-wide entries are visible to other roles in that project but not to a
  different project. Organization-role entries are visible to that role across
  projects in the same organization and nowhere else.
- Every entry and revision contains a permitted authoritative source reference
  and observed version. Missing or already-stale sources reject writes.
- When a source advances or is removed, current-memory reads stop returning the
  entry and inspection reads expose `stale` or `removed` with the latest source
  version; history is retained.
- Concurrent updates using one expected version produce one success and one
  explicit conflict. Exact idempotent retries do not create extra revisions.
- No schema, API, record, or query accepts a provider thread, turn,
  conversation, prompt, or role-instance id as shared-memory authority.
