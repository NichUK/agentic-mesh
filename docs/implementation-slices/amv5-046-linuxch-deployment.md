# AMV5-046 — Canonical LinuxCH V5 deployment

## Outcome

Make the project-owned Compose deployment the canonical Linux Docker topology
for Agentic Mesh V5 on the LinuxCH host. The deployment contains the complete
core stack and uses external project-scoped state, configuration, Codex OAuth,
API tokens, and GitHub authentication.

Windows Docker remains a development and qualification environment. It is not
the canonical production topology.

## Scope and boundaries

- Keep one explicit Docker Compose project; do not add Kubernetes or another
  orchestrator.
- Include Postgres, one migration job, the control API, OpenTelemetry, and the
  complete pre-provisioned 15-role/16-instance fleet.
- Reuse the six project-neutral V5 image classes. Role/persona prompts remain
  external and are selected by each role-service command.
- Keep Postgres authoritative. Store its live data outside Git in the
  project-owned state root.
- Pin every runtime and worker image by digest through external deployment
  variables. Never mount the Agentic Mesh runtime source into the control
  image.
- Mount the ordinary project source checkout and isolated worktree root only
  into workers that must perform project work.
- Reuse the current Codex OAuth login by seeding an external per-instance
  `CODEX_HOME`; do not put OAuth data in Git or images.
- Give engineering instances a project-scoped, read-only GitHub CLI credential
  directory and a non-secret Git credential-helper configuration. Do not mount
  a general host credential directory.
- Keep role API tokens, Postgres password, principal hashes, and connector
  credentials outside Git under the project state boundary.
- Keep external SaaS systems such as GitHub, Azure DevOps, OneDrive, Teams and
  Figma outside the Compose stack.

## Testing plan

1. Validate the Compose model and required environment contract.
2. Prove Postgres, migration, control, telemetry, and all 16 role instances are
   present and project-scoped.
3. Prove fleet-map container identities exactly match the Compose services.
4. Prove workers use the expected general, development, QA, operations, or UX
   image without embedding a persona.
5. Prove every worker mounts external configuration, source bindings,
   workspaces, its own Codex home, and its role API token.
6. Prove only engineering instances receive the project-scoped GitHub
   credential mounts, and no GitHub token environment variable exists.
7. Run `docker compose config --quiet` with non-secret qualification values.
8. Deploy to LinuxCH, migrate the empty database, and verify service health,
   queue pickup, warm role execution, hibernation, wake-up, and PM continuity.

## Acceptance criteria

- The checked-in project deployment starts the complete V5 core on Linux
  Docker without any V4 artifact or source bind mount in the control service.
- Postgres is authoritative and the migration job succeeds before control and
  worker services become healthy.
- All 16 declared fleet instances have an exact project/role/instance command,
  external token file, isolated Codex home, and matching fleet-map identity.
- Engineering can authenticate to the declared Git repository, push an
  isolated branch, and create a PR using the project-scoped external GitHub
  credential. Other projects cannot resolve that mount.
- Prompt, role, flow, memory, source, document, secret, and runtime-state
  boundaries remain explicit and project-isolated.
- The deployment passes LinuxCH health and live self-development qualification
  before V5 receives operational control of its own backlog.
