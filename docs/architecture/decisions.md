# Architecture Decisions

Architecture decisions are recorded here and linked back to design documents,
product scope, enterprise constraints, implementation plans, and rollback
notes.

## ADR-001 - Agentic Mesh Enterprise Runtime Direction

Date: 2026-06-02

Status: draft direction

Design document: `docs/architecture/agentic-mesh-design.md`

### Context

The OpenAgents-centered prototype proved useful concepts around live role
agents, channel handoffs, approvals, sponsor DMs, and documentation ownership,
but it also exposed repeated friction in the substrate. Basic product
primitives such as identity, direct messages, channel state, action rendering,
project boundaries, and UI behavior required direct fixes or workarounds.

The target product is broader than a dev-team-only tool and more
enterprise-focused. It needs long-running role agents, container isolation,
project-scoped networks, Microsoft Teams as the first human UI, OpenTelemetry
observability, Git-backed configuration and documentation, pluggable storage,
pluggable collaboration connectors, and pluggable worker/model providers.

The runtime must also support permanent role templates that are rarely
updated, project-specific overrides for those roles, and multiple instances of
the same role inside one project.

### Decision

Adopt **Agentic Mesh** as the new architecture direction.

Agentic Mesh is a containerized, project-scoped role-agent runtime. Each
role-agent instance runs in its own container with its own tools,
configuration, instructions, queue, journal, and storage volume. Agents
communicate through a router and durable message/storage ports rather than
through a central executive controller.

Role definition is split into:

- permanent role templates
- organization-level defaults
- project-level overrides
- role-instance configuration

This allows a company to reuse stable roles across many projects while
tailoring instructions, tools, storage, connectors, and scaling per project.

Microsoft Teams is the first collaboration connector. A project maps to a
Team, and role surfaces map to channels. The connector abstraction must remain
generic enough for Slack and other collaboration surfaces.

Storage is split into message, state, artifact, and event-journal ports.
Local development defaults to file-backed append-only queues and inspectable
state. Enterprise deployments may use cloud-native backends such as Azure
Service Bus, Azure Table Storage, Azure Blob Storage, DynamoDB, SQS/SNS, or
S3. Every deployment must retain an append-only event journal for audit and
replay.

Worker execution is abstracted behind a `WorkerAdapter` so Codex, OpenAI,
Anthropic, Claude Code, MiniMax, DeepSeek, and future providers can be used
without changing product-level workflow semantics.

### Consequences

- OpenAgents remains a prototype reference and upstream contribution target,
  not the foundation for the next product architecture.
- Docker Compose becomes the first local deployment target; Terraform or
  cloud-native deployment profiles can follow.
- Teams connector work becomes first-class product scope rather than a
  notifier afterthought.
- Runtime queues and event journals are inspectable by default, while cloud
  backends can provide enterprise reliability.
- Git remains authoritative for configuration, documentation, decisions,
  stories, evidence, and release records.
- Database usage is minimized and abstracted; when used, it is a backend
  choice rather than a product assumption.
- OTEL telemetry is required from the first implementation slice.
- Role templates can be governed centrally while project overrides remain
  project-owned and Git-versioned.
- Multiple instances of one role can scale work within a project without
  redefining the role itself.

### Alternatives Considered

- Continue hardening OpenAgents: rejected as primary direction because the
  architecture has repeatedly fought core product semantics.
- Adopt another agent framework wholesale: deferred because Agentic Mesh needs
  enterprise runtime, connector, storage, role-governance, and documentation
  semantics more than a generic agent conversation framework.
- Use Azure Service Bus as the core architecture: rejected as the default
  because it is strong for delivery but too opaque as the only audit and debug
  surface. It remains an enterprise backend option behind the message port.
- Use Git as the only runtime queue: rejected for high-frequency queue traffic,
  retries, and claims. Git remains the configuration and artifact authority.

### Rollback

Rollback for this decision means continuing with the current OpenAgents-based
prototype while preserving the Agentic Mesh design as future architecture
research. Do not delete OpenAgents prototype scripts or docs until the first
Agentic Mesh slice proves equivalent or better live role-agent behavior.

### Delivery Handoff

Delivery should treat `docs/architecture/agentic-mesh-design.md` as the
candidate architecture baseline for the next product direction. The first
implementation slice should prove a Docker Compose topology with router,
Product, Engineering, file-backed message/state/artifact/journal adapters,
Codex worker adapter, permanent role templates, project overrides, role
multi-instancing, and OTEL traces before expanding to Teams, additional roles,
cloud storage, or Service Bus.

