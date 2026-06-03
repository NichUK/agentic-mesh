# Local Runtime Skeleton v0

Status: accepted for first development slice

Date: 2026-06-02

## Product Goal

Prove the Agentic Mesh enterprise spine locally before adding real Teams,
cloud queues, control-plane UI, or provider-specific agent execution.

This slice should demonstrate that a project can declare stable role
templates, project overrides, multiple instances of one role, inspectable local
queues, append-only journaling, deterministic work claiming, and queue-aware
lifecycle transitions.

The SDLC flow is project configuration, not runtime policy and not role-template
policy. Role templates describe durable role behavior. A project flow overlay
declares which lifecycle states exist, which role owns each state, and where
completed work should be handed next.

## Scope

- Python runtime skeleton with explicit ports for config, message delivery,
  artifact writing, event journaling, worker execution, and lifecycle state.
- Central organization defaults in `config/organization.yaml`, including
  `global_language`, documentation, conversation, handoff, and security
  defaults.
- Central auth method catalog in `config/auth-methods.yaml` and per-role worker
  auth bindings in project config.
- Central human response type catalog in `config/response-types.yaml`.
- Agentic Mesh development project configuration in
  `examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml`.
- Reusable SDLC flow template in `config/flows/sdlc.yaml`, referenced by the
  example project instead of embedded in the project overlay.
- Project configuration schema in `config/schemas/project.schema.json`.
- Response type catalog schema in `config/schemas/response-types.schema.json`.
- Starter SDLC role templates for Business Analyst, Product Manager, UX
  Designer, Enterprise Architect, Solution Architect, Security Architect,
  Platform Engineer, Engineering, QA Engineer, Technical Writer, Delivery
  Manager, Research Analyst, and Release Manager.
- Project SDLC flow overlay support for slices, features, and spikes.
- File-backed local message store using inspectable JSON payloads.
- Append-only JSONL event journal.
- Configured worker adapter dispatch with `codex-cli` as the first real worker
  path and a deterministic worker test double for unit tests.
- Basic control-plane tick that hibernates idle instances and wakes hibernated
  instances when role inbox work exists.
- Docker Compose topology for router, control-plane, the configured
  `agentic-mesh-dev` role agents, and OTEL collector.

## Acceptance Criteria

- Project config loads and validates role template references.
- Organization defaults load and expose `global_language` and related
  cross-project standards.
- Auth methods load and role worker auth bindings validate against supported
  adapters and required secret or mount references.
- Response type templates load and human response gates validate referenced
  response types.
- Project config loads and validates a referenced SDLC flow template.
- Project YAML shape is documented by a schema under `config/schemas/`.
- Every flow state owner and handoff target must be a configured project role.
- Role instances derive from permanent role templates plus project override.
- `agentic-mesh-dev.engineering.1` and `agentic-mesh-dev.engineering.2` share the
  Engineering role queue while preserving distinct role-instance ids.
- Two Engineering instances cannot claim the same queued message.
- A work item message carries `work_item_id`, `work_item_type`, and
  `lifecycle_state`.
- A work item intake message can be written to the role that owns the configured
  entry state.
- Business Analyst can claim a `business_analysis` item, write the configured
  artifact, record journal events, and emit the configured handoff to
  `product_definition`.
- Product Manager can claim `product_definition` work and emit the configured
  handoff to the next state.
- Solution Architect can claim `solution_design` work and emit the configured
  handoff to the next state.
- One Engineering instance can claim `implementation` work, write the
  configured implementation artifact, and complete the work.
- Multiple slices, features, and spikes can be queued and handed off in
  parallel while preserving separate `work_item_id` and lifecycle context.
- The event journal records message accepted, work claimed, agent run started,
  documentation updated, handoff emitted, message routed, work completed,
  hibernate, and wake events.
- Lifecycle state preserves stable role-instance ids across idle, hibernated,
  and wake transitions.
- A local trace stand-in records correlation ids through the same journaled
  flow while OTEL collector wiring exists in Compose for the next slice.

## Non-Goals

- Real Codex, OpenAI, Anthropic, Claude Code, MiniMax, or DeepSeek execution.
- Real Microsoft Teams connector behavior.
- Cloud queue or storage backends.
- Control-plane UI.
- Full SDLC role pack.
- Stock enterprise flow catalog research or templates.
- Container orchestration beyond a local Docker Compose topology.
