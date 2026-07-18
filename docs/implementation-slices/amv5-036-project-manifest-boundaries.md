# AMV5-036 — Project manifest and resource boundaries

## Outcome

Define one small V5 `agentic-mesh/project.yaml` contract for the resources a
project may use. Git owns the manifest and its history. Postgres stores an
immutable, validated snapshot and one current digest so every runtime action
can be tied to the exact configuration it used.

## Scope and boundaries

- Keep this schema separate from the large V4-era project overlay. V5 does not
  import V4 runtime or workflow mechanics.
- Describe multiple Git repositories, OneDrive document roots, Teams channels,
  one ADO organization/project binding, external credential references,
  project package overrides, per-role tool profiles, and instance limits.
- Store identifiers and references only. Reject embedded credentials, URL user
  information, environment interpolation, unknown fields, and malformed paths.
- Give every external resource a project owner. A resource owned by another
  project or by the organization requires an exact external authorization
  grant; the manifest never contains grant or credential values.
- Treat project-scoped credential names as isolated even when two projects use
  the same logical name. Organization-scoped credentials require an explicit
  grant.
- Detect collisions with resources in every other active project snapshot.
  Sharing is allowed only through an exact grant naming target project, owner,
  resource kind, canonical resource key, and authorization reference.
- Validate package references with the existing V5 package-reference grammar.
  Package resolution and activation remain in the organization configuration
  repository; the project manifest only pins the organization release and
  project overrides.
- Persist the canonical snapshot, source commit, source path, resource claims,
  actor, and digest atomically. Activation uses optimistic expected-digest
  checks and records a project audit entry.
- Do not implement Git worktrees, OneDrive operations, memory, Teams delivery,
  ADO synchronization, or role activation in this slice; those consumers follow
  in AMV5-037 through AMV5-042 and AMV5-048 through AMV5-052.

## Acceptance criteria

- A valid multi-repository project manifest produces the same SHA-256 digest
  and normalized snapshot on every load.
- Unknown fields, duplicate/colliding resources, invalid package/tool-profile
  references, unsafe URLs/paths, unresolved credential ids, embedded secret
  material, and inconsistent instance limits fail closed.
- Project credentials are namespaced to the project. Organization credentials
  and foreign resources fail without an exact external grant.
- Two active projects cannot claim the same repository, document root, Teams
  channel, or ADO project unless the later project supplies an exact grant.
- Postgres retains immutable historical snapshots and claims while one current
  pointer advances atomically with an audit record. Exact retries are
  idempotent and stale expected-digest updates are rejected.
- The runtime snapshot contains credential references, never credential values,
  and source provenance identifies the exact Git commit and
  `agentic-mesh/project.yaml` path.
