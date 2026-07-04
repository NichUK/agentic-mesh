# Agentic Mesh

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

The active development direction is the V4 remote-control runtime reset. V4
replaces the V3 broker-wrapped worker model with one Codex app-server
remote-control container per configured role instance.

## Current Runtime Direction

V4 keeps lifecycle judgement with role agents and reduces the runtime to
infrastructure: Teams/API ingress, Postgres-backed message queues, Codex
app-server WebSocket routing, safe-output tools for durable workflow effects,
dashboard/reporting, hibernation/wake coordination, and telemetry.

The V4 spine currently provides:

- the full SDLC starter role set
- one generated external `AGENTS.md` per role instance
- one Codex app-server container per role instance
- Postgres queue/state tables for messages, sessions, threads, turns, events,
  safe-output calls, memory, artifacts, approvals, handoffs, and releases
- app-server protocol client boundaries for `thread/start`, `thread/resume`,
  `turn/start`, `turn/steer`, `turn/interrupt`, `thread/read`, and paginated
  history reads
- `/status`, `/agents`, `/work-item/{id}`, `/agent/{role}/thread`, and
  `/artifact-viewer/{work-item-id}/{artifact}` reporting primitives

The current dogfood status page is:

```text
http://linuxch:8100/status
```

Useful local commands:

```powershell
pip install -e .[dev]
pytest -q
$env:AGENTIC_MESH_DATABASE_URL="postgresql://agentic_mesh:password@localhost:5432/agentic_mesh_v4"
agentic-mesh-v4 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml init-db
agentic-mesh-v4 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml materialize-agent-configs --agent-config-root .tmp/v4-agents --role-templates-dir config/roles
agentic-mesh-v4 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml render-compose --output .tmp/docker-compose.v4.yml
agentic-mesh-v4 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml enqueue-message --target-role project-manager --text "Give me a status update"
agentic-mesh-v4 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml status-json
```

The `project-v4.yaml` dogfood command expects the V4 environment to provide
Postgres connection settings, Graph/Teams credentials, Codex auth mounts,
`AGENTIC_MESH_ONEDRIVE_TOKEN`,
`AGENTIC_MESH_ONEDRIVE_DRIVE_ID`, and `AGENTIC_MESH_SPONSOR_TEAMS_USER_ID`.

Validate source/runtime/project boundaries with the active V4 topology rules:

```powershell
agentic-mesh-v4 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml status-json
```

The earlier `validate-topology` command remains in legacy packages until it is
ported to V4, but new work should target the V4 package and project topology.

## Repository Layout

- `src/agentic_mesh_v4/`: active V4 remote-control runtime package
- `src/agentic_mesh_v3/`: previous V3 runtime package, retained temporarily
- `src/agentic_mesh_v2/`: frozen V2 runtime package, retained temporarily
- `tests/test_v4_*.py`: active V4 regression tests
- `tests/test_v3_*.py` and `tests/test_v2_*.py`: legacy regression coverage
- `examples/projects/agentic-mesh-dev/deploy/compose/`: dogfood compose
  deployment, currently V4-first
- `docs/architecture/v4-remote-control-runtime.md`: V4 reset architecture
- `docs/architecture/v3-agent-owned-runtime.md`: previous V3 reset architecture
- `docs/architecture/v2-runtime-reset.md`: previous v2 reset plan and topology
  direction
- `docs/architecture/repository-topology.md`: source/runtime/project boundary
  guidance
- `docs/architecture/risk-register.md`: risks captured during dogfood
  recovery

## Standards-Informed Roles And Flows

Agentic Mesh adapts established enterprise and professional standards into
configurable role templates, project flows, gates, and document
accountabilities. The standards guide defaults; they do not lock every project
into one methodology.

- BMAD informs specialist AI agent roles, named workflows, role-specific
  capabilities, and artifact-driven handoffs.
- Scrum informs accountability boundaries, product ownership, developer
  planning ownership, flow transparency, and impediment handling.
- SFIA informs focused professional skill profiles.
- BABOK informs Business Analyst work such as stakeholder context and
  requirements lifecycle thinking.
- TOGAF informs architecture alignment, governance, capability fit, and
  decision traceability.
- ISTQB and BDD practice inform QA planning and evidence handling.
- OWASP SAMM and NIST SSDF inform secure SDLC expectations and residual-risk
  recording.
- Prompt engineering practice informs prompt contracts, safe-output tool
  instructions, context boundaries, and behavioural regression scenarios for
  role agents.

## Product Positioning

Agentic Mesh is intended to be open core.

Open source core should remain useful on its own:

- V4 remote-control runtime kernel
- Teams connector, document-library, safe-output, and Codex app-server adapter ports
- governance-aware role agents and RACI
- Postgres local backend
- Docker Compose deployment profile
- documentation framework primitives
- status/reporting dashboard
- starter role and flow templates
- prompt-engineering role support for agent prompt contracts and behavioural
  diagnosis

Commercial offerings may add supported enterprise deployments, SSO/RBAC,
managed storage/message backends, policy packs, compliance reporting, premium
support, and assisted operations.

The open source project should not be crippleware. Commercial features should
improve enterprise adoption, governance, reliability, and support rather than
locking away the basic agent network.
