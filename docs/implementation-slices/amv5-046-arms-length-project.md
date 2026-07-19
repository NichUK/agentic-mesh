# AMV5-046 — Register V5 as an arms-length project

## Outcome

Register V5 through the project manifest, role-pack, runtime boundary, and
isolated-worktree mechanisms used by any project. V5 may build and test a
candidate, but cannot mutate the running install or state from its workspace.

## Scope and boundaries

- Add one generic project registration coordinator; do not add a self-hosting
  mode or V5-specific workflow path.
- Require an active external configuration release at an exact Git revision,
  then activate the ordinary project manifest and role pack.
- Persist the manifest/configuration pins, digest-pinned image, deployment
  adapter, and install/state/workspace paths. Never store credentials.
- Require configuration, running install, runtime state, and workspaces to be
  pairwise disjoint. Runtime install/state/workspace roots cannot overlap the
  corresponding roots of another project.
- Prepare project work only through the existing `GitWorktreeCoordinator` and
  reject source repositories that overlap any protected runtime boundary.
- Keep the running image immutable. Candidate build, test, deployment, restart,
  verification, and rollback remain separate; AMV5-047 owns upgrade mechanics.
- Put the V5 project-specific instructions in the external configuration
  repository and keep the checked-in project manifest secret-free.
- Expose the same operation through the bootstrap `project-register` CLI so an
  operator can register or exactly replay a project without a dashboard.

The concrete declaration is `examples/projects/agentic-mesh-v5/agentic-mesh/project.yaml`.
Its external `project-override/agentic-mesh-v5@0.1.0` package repeats the
simplicity and live-runtime isolation rules.

## Acceptance criteria

- Exact registration is idempotent and conflicting identity, sponsor, active
  configuration, revision, image, manifest, or path data fails closed.
- V5 source work occurs only in a durable external worktree tied to the active
  manifest; the source checkout, running install, and runtime state remain
  unchanged during a real committed candidate change and test.
- A coordinator reconstructed after a simulated runtime restart returns the
  same registration and worktree plan.
- A candidate revision can be built/tested without being treated as the running
  revision or mutating the digest-pinned running image.
- An engineering instance can authenticate to the manifest's Git repository,
  push its isolated feature branch, and open a review PR through the declared
  project-scoped `git` credential. The credential is mounted externally and
  read-only; its value never enters Git, an image, container environment,
  database, log, progress record, or test evidence.
- Recreated and scaled engineering instances receive the same project-scoped
  Git capability without copying credentials into role memory. A different
  project cannot resolve or mount the credential, and missing authentication
  fails before source mutation or a false handoff.
- Another project cannot claim or nest within V5 install, state, or workspace
  roots, while the organization configuration root may be shared read-only.

## Live takeover credential preflight

The 2026-07-19 live fleet proves external API-token and Codex OAuth delivery
for all 16 current roles. A clean development image preflight also proves that
GitHub authentication and a Git credential helper are absent. V5 therefore is
not yet authorized to take over its own development even though queue pickup,
completion, recovery, and hibernation qualification passed.

The correction must be made in the authoritative project deployment so every
future engineering instance receives the manifest's `git` credential. Do not
patch a token into the current container, use an environment variable, or
reuse an unscoped host credential mount. The sponsor's pending topology and
canonical-input decisions determine which project-owned deployment artifact
materializes the external read-only mount.
