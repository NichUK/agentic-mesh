# Architecture

Agentic Mesh is a project-scoped mesh of role agents, collaboration
connectors, storage backends, worker/model adapters, and observable runtime
services.

The current architecture direction is recorded in:

- `docs/architecture/agentic-mesh-design.md`
- `docs/architecture/decisions.md#adr-001---agentic-mesh-enterprise-runtime-direction`

## Direction Summary

Agentic Mesh replaces the OpenAgents-centered prototype direction with an
enterprise runtime designed around independently deployable, long-running role
agents.

Each role-agent instance runs in its own container with:

- role identity
- role configuration
- project override configuration
- standing instructions
- approved tools
- role-specific storage volume
- inbox, outbox, and journal
- OpenTelemetry service identity
- lifecycle policy for idle hibernation and automatic wake-up

Role definitions are separated into permanent role templates and
project-specific overrides. A company can reuse the same base roles across
many projects while adjusting local instructions, tool access, connectors,
write boundaries, and deployment scale per project.

Projects may run multiple instances of the same role where needed. The runtime
must treat role template, role instance, and project assignment as separate
concepts.

Idle role-agent instances can hibernate after a configurable grace period.
The control-plane wakes hibernated instances when new role work, DMs, mentions,
scheduled work, or manual operator action requires them. This keeps local and
cloud-native installations resource-efficient without losing durable queue
messages or role state.

Agentic Mesh is intended as an open source project with a commercial offering
on top. The open source core should remain useful on its own. Commercial
features can focus on enterprise support, advanced control-plane capabilities,
SSO/RBAC, managed cloud deployment, policy packs, compliance reporting, and
operational support.

## Core Boundaries

- Agent runtime: claims work, loads context, enforces policy, invokes worker
  adapters, records outcomes, and emits telemetry.
- Worker adapter: executes model or CLI work through Codex, OpenAI, Anthropic,
  Claude Code, MiniMax, DeepSeek, or future providers.
- Router: routes messages, handoffs, DMs, action requests, and connector
  events. It is not an executive controller.
- Control-plane: supervises topology, role-instance lifecycle, hibernation,
  wake-up, health checks, and configuration reloads.
- Collaboration connector: maps Teams, Slack, web, CLI, or other surfaces to
  the internal message/action model.
- Message store: owns durable delivery, claims, retries, and dead letters.
- State store: owns project state, work item state, leases, cursors, and
  approvals.
- Artifact store: owns docs, decisions, stories, test evidence, release
  records, and generated artifacts.
- Event journal: records an append-only audit/replay history regardless of
  queue backend.
