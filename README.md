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

