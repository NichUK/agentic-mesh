# Agentic Mesh

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

The active development direction is the V3 agent-owned runtime reset. V2 remains
the last dogfood deployment until V3 proves the first end-to-end slice and is
deployed intentionally.

## Current Runtime Direction

V3 moves lifecycle judgement out of the runtime and into self-contained,
long-running role agents. The runtime becomes a platform kernel for startup,
hibernation, broker access, connector bridges, document-library access,
reporting, configuration materialisation, and telemetry.

The V3 spine currently provides:

- explicit SDLC RACI and governance rules
- a Project Manager role separate from Delivery Manager
- topology validation for source/runtime/project/document boundaries
- a broker adapter port with a NATS JetStream target and local test adapter
- a self-contained role-agent service loop
- V3 tool calls for backlog, work items, documents, approvals, releases, and status
- a OneDrive/SharePoint document-library adapter boundary with Graph-backed implementation
- a Teams bridge boundary with local and Graph-backed outbound implementations
- `/status`, `/agents`, `/work-item/{id}`, and `/artifact-viewer/{id}/{filename}` reporting primitives
- a local demo slice proving queue/work/agents/artifacts/release/closure projections

The current dogfood status page is still the V2 runtime:

```text
http://linuxch:8100/status
```

Useful local commands:

```powershell
pip install -e .[dev]
pytest -q
agentic-mesh-v3 --db .tmp/v3.sqlite3 init-db
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev demo-slice --document-library-root .tmp/v3-documents
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev status-json
agentic-mesh --db .tmp/v2.sqlite3 init-db
agentic-mesh --db .tmp/v2.sqlite3 demo-slice
agentic-mesh --db .tmp/v2.sqlite3 status-json
```

Validate source/runtime/project boundaries:

```powershell
agentic-mesh-v3 validate-topology `
  --source-repo C:\Dev\agentic-mesh `
  --deployed-runtime C:\AgenticMesh\runtime `
  --runtime-state C:\AgenticMesh\state `
  --organisation-config-repo C:\AgenticMesh\org-config `
  --project-config-repo C:\Projects\demo-config `
  --document-library-root C:\Users\you\OneDrive\AgenticMesh\documents
```

## Repository Layout

- `src/agentic_mesh_v3/`: active V3 runtime reset package
- `src/agentic_mesh_v2/`: frozen V2 runtime package, retained while V3 is proved
- `tests/test_v3_*.py`: active V3 regression tests
- `tests/test_v2_*.py`: active v2 regression tests
- `examples/projects/agentic-mesh-dev/deploy/compose/`: dogfood compose
  deployment, currently v2-only
- `docs/architecture/v3-agent-owned-runtime.md`: V3 reset architecture
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

- V3 agent-owned runtime kernel
- broker, connector, document-library, and worker adapter ports
- governance-aware role agents and RACI
- V2 state machine and safe-output service while V2 remains available
- SQLite local backend
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
