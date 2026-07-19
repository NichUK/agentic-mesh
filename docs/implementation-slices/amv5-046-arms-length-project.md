# AMV5-046 — Register V5 as an arms-length project

## Outcome

Register Agentic Mesh V5 through the same project manifest, role-pack, runtime
boundary, and isolated-worktree mechanisms used by any other project. V5 may
build and test a candidate replacement, but cannot mutate the running install
or runtime state through its source workspace.

## Scope and boundaries

- Add one generic project registration coordinator; do not add a self-hosting
  mode or V5-specific workflow path.
- Require an active external configuration release at an exact Git revision,
  then activate the ordinary project manifest and role pack.
- Persist the manifest digest, configuration revision/root, digest-pinned
  running image, running-install root, runtime-state root, workspace root, and
  deployment adapter. Store references and paths only, never credentials.
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

The concrete V5 declaration is
`examples/projects/agentic-mesh-v5/agentic-mesh/project.yaml`. Its
`project-override/agentic-mesh-v5@0.1.0` package remains in the separate
organization configuration repository and repeats the simplicity and live
runtime isolation rules.

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
- Another project cannot claim or nest within V5 install, state, or workspace
  roots, while the organization configuration root may be shared read-only.
