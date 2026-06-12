# Agentic Mesh

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

The active branch is the v2 runtime reset. V1 has been removed from the source
package and the linuxch dogfood deployment now runs the v2 runtime service on
port `8100`.

## Current Runtime

V2 is SQLite-backed and uses explicit safe-output records as the durable source
of truth for agent work. The current MVP proves:

- queue item capture
- work item promotion
- product, engineering, QA, and release role runs
- safe-output recording
- artifact and release evidence
- work item closure only after release evidence exists
- a v2 status/reporting dashboard

The dogfood status page is:

```text
http://linuxch:8100/status
```

Useful local commands:

```powershell
pip install -e .[dev]
pytest -q
agentic-mesh --db .tmp/v2.sqlite3 init-db
agentic-mesh --db .tmp/v2.sqlite3 demo-slice
agentic-mesh --db .tmp/v2.sqlite3 status-json
```

Validate source/runtime/project boundaries:

```powershell
agentic-mesh validate-topology `
  --source-repo C:\Dev\agentic-mesh `
  --deployed-runtime C:\AgenticMesh\runtime `
  --runtime-state C:\AgenticMesh\state `
  --project-repo demo=C:\Projects\demo|C:\Projects\demo\docs
```

## Repository Layout

- `src/agentic_mesh_v2/`: active v2 runtime package
- `tests/test_v2_*.py`: active v2 regression tests
- `examples/projects/agentic-mesh-dev/deploy/compose/`: dogfood compose
  deployment, currently v2-only
- `docs/architecture/v2-runtime-reset.md`: v2 reset plan and topology
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

## Product Positioning

Agentic Mesh is intended to be open core.

Open source core should remain useful on its own:

- v2 runtime kernel
- state machine and safe-output service
- SQLite local backend
- Docker Compose deployment profile
- documentation framework primitives
- status/reporting dashboard
- starter role and flow templates

Commercial offerings may add supported enterprise deployments, SSO/RBAC,
managed storage/message backends, policy packs, compliance reporting, premium
support, and assisted operations.

The open source project should not be crippleware. Commercial features should
improve enterprise adoption, governance, reliability, and support rather than
locking away the basic agent network.
