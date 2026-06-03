# Agentic Mesh

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

The product is not tied to a single model provider, collaboration tool, queue
service, database, or agent framework. It is designed around replaceable
adapters for workers, storage, collaboration connectors, and deployment
profiles.

Current documentation:

- `docs/architecture/agentic-mesh-design.md`: main design document
- `docs/architecture/repository-topology.md`: system repo and project repo
  boundary
- `docs/architecture/authentication.md`: provider-neutral worker and connector
  auth model
- `docs/architecture/document-lifecycle.md`: document accountability,
  contribution events, and owner-review gates
- `docs/architecture/flow-collaboration.md`: consult routes and
  sponsor-initiated tracked work
- `docs/architecture/human-response-gates.md`: structured human response gates
  and reusable response type templates
- `docs/architecture/decisions.md`: architecture decisions
- `docs/architecture.md`: architecture overview
- `docs/operations/codex-auth.md`: Codex auth strategy for role containers
- `docs/operations/git-workflow.md`: daily `develop` branch and PR workflow
- `docs/operations/teams-bot-identities.md`: one Teams bot identity per role
  for the dogfood project
- `docs/operations/linuxch-docker-deployment.md`: Docker deployment profile
  and Teams bot ingress notes for the `linuxch` VM
- `docs/operations/signoz-otel.md`: OpenTelemetry routing, naming, and SigNoz
  trace search notes
- `docs/operations/sdlc-teams-smoke-test.md`: latest SDLC lifecycle smoke test
  through containers and Microsoft Teams
- `docs/product/open-source-commercial-plan.md`: open source core,
  commercial boundary, quick wins, and license recommendation
- `docs/research/auth-methods-spike.md`: initial auth research spike
- `docs/implementation-slices/local-runtime-skeleton-v0.md`: first
  implementation slice and acceptance criteria
- `docs/implementation-slices/local-messaging-v0.md`: local role inbox,
  connector outbox, and human response message slice
- `docs/implementation-slices/local-teams-connector-v0.md`: local Teams-style
  connector adapter and Adaptive Card rendering slice

Initial direction:

- one container per role-agent instance
- central organization defaults such as global language, documentation,
  conversation, handoff, and security standards
- permanent role templates with project-specific overrides
- reusable flow templates with per-project flow references and overrides
- reusable human response type templates for approvals, yes/no, numbers,
  money, text, documents, and URLs
- project configuration schemas for validation and future UI editing
- support for multiple instances of the same role inside one project
- Microsoft Teams as the first collaboration connector
- one Teams bot identity per configured role for the dogfood Teams project
- Slack-ready connector abstraction
- file-first local storage with cloud-native storage backends
- append-only event journal for audit and replay
- OpenTelemetry logs, traces, and metrics
- Git-backed configuration, documentation, decisions, and evidence
- queue-aware agent hibernation and wake-up for resource efficiency
- open source core with commercial enterprise support and extensions

## Product Positioning

Agentic Mesh uses role discipline inspired by BMAD-style software delivery
packs, but its runtime architecture is distributed, containerized, and
enterprise-oriented.

Credit: Agentic Mesh draws inspiration from the
[BMad Method](https://docs.bmad-method.org/) and its role-guided software
delivery workflows. Agentic Mesh does not copy BMAD's named-agent convention;
role templates use functional titles such as Business Analyst, Product
Manager, Solution Architect, Engineering, QA Engineer, and Release Manager.

The intended model is open core:

- The core runtime, role template model, local storage adapters, Docker Compose
  profile, worker adapter interface, collaboration connector interface, and
  basic Teams connector should be open source.
- Commercial offerings may add supported enterprise deployments, advanced
  control-plane features, SSO and identity integrations, managed cloud
  backends, policy packs, compliance reporting, premium support, and hosted or
  assisted operations.

The open source project should be useful on its own. Commercial features should
pay for development by improving enterprise adoption, governance, reliability,
and support rather than by locking away the basic agent network.

## Local Runtime Skeleton

The first development slice is a Python source-layout project. To run it
directly from a checkout:

```powershell
$env:PYTHONPATH = "src"
python -m agentic_mesh.cli validate-config
python -m agentic_mesh.cli auth-plan --instance agentic-mesh-dev.security-architect.1
python -m agentic_mesh.cli record-human-response --work-item-id slice-release --lifecycle-state release_review --gate-id release_decision_response --response-request-id human-response-example --responder release-sponsor --value '"approved"'
python -m agentic_mesh.cli teams-connector-once --channel approvals
python -m agentic_mesh.cli status
pytest -q
```

Or install it in editable mode:

```powershell
pip install -e .[dev]
python -m agentic_mesh.cli validate-config
```

The default local example project is `agentic-mesh-dev`. It references the
stock SDLC flow template in `config/flows/sdlc.yaml`; role templates do not
hard-code the handoff graph. Project YAML shape is documented in
`config/schemas/project.schema.json`.

Each project YAML declares a `workspace` block with the mounted project root
and repository entries agents can work in. Role `write_paths` and flow
`artifact_path` values are relative to that workspace, so the shared runtime
image can stay generic while each deployment mounts its own project repo,
configuration, state, and secrets.

Global defaults such as `global_language` live in `config/organization.yaml`.
Reusable human response templates live in `config/response-types.yaml`.
