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

Role definitions are separated into permanent role templates and
project-specific overrides. A company can reuse the same base roles across
many projects while adjusting local instructions, tool access, connectors,
write boundaries, and deployment scale per project.

Projects may run multiple instances of the same role where needed. The runtime
must treat role template, role instance, and project assignment as separate
concepts.

## Core Boundaries

- Agent runtime: claims work, loads context, enforces policy, invokes worker
  adapters, records outcomes, and emits telemetry.
- Worker adapter: executes model or CLI work through Codex, OpenAI, Anthropic,
  Claude Code, MiniMax, DeepSeek, or future providers.
- Router: routes messages, handoffs, DMs, action requests, and connector
  events. It is not an executive controller.
- Collaboration connector: maps Teams, Slack, web, CLI, or other surfaces to
  the internal message/action model.
- Message store: owns durable delivery, claims, retries, and dead letters.
- State store: owns project state, work item state, leases, cursors, and
  approvals.
- Artifact store: owns docs, decisions, stories, test evidence, release
  records, and generated artifacts.
- Event journal: records an append-only audit/replay history regardless of
  queue backend.

