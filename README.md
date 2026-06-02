# Agentic Mesh

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

The product is not tied to a single model provider, collaboration tool, queue
service, database, or agent framework. It is designed around replaceable
adapters for workers, storage, collaboration connectors, and deployment
profiles.

Current documentation:

- `docs/architecture/agentic-mesh-design.md`: main design document
- `docs/architecture/decisions.md`: architecture decisions
- `docs/architecture.md`: architecture overview

Initial direction:

- one container per role-agent instance
- permanent role templates with project-specific overrides
- support for multiple instances of the same role inside one project
- Microsoft Teams as the first collaboration connector
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
