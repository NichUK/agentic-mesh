# Agentic Mesh V3 Agent-Owned Runtime

V3 is a hard reset toward self-contained role agents and a thinner runtime.
The runtime provides platform services; agents own work progression.

## Boundary

- Source repo: Agentic Mesh product code, templates, tests, schemas, and docs.
- Deployed runtime: installed artifact or image plus operational mounts.
- Organisation config repo: organisation defaults, identity, policy, and connector defaults.
- Project config repo: project role instances, RACI, channels, targets, and overrides.
- Document library: project memory and evidence, initially OneDrive mounted into Teams as Shared Files under `/documents`.

These paths must remain distinct unless a local-dev override is explicit.

## Runtime Responsibilities

- Start, stop, hibernate, and hydrate role-agent containers.
- Provide broker, connector, document-library, configuration, and telemetry services.
- Materialise configuration into agent-readable files.
- Maintain stakeholder reporting read models.
- Wake an agent when an inbox message arrives.

The runtime must not make product, architecture, implementation, QA, release,
or governance decisions. Those decisions belong to role agents operating under
RACI and sponsor instructions.

## Agent Responsibilities

Each role instance is a long-running service with:

- mounted role, organisation, project, RACI, and tool configuration
- broker-backed inbox/outbox
- DB-backed role memory
- access to source repositories granted by project config
- access to the document-library adapter
- prompt assembly from instructions, memory, governance, and current work

Agents must use tools for durable effects and must confirm meaningful action
after doing the work. If no action is appropriate, they must explicitly call a
no-op/status tool and explain why.

Agent prompts are assembled from mounted role, organisation, project, RACI, and
tool-context files plus governance instructions, current assignment context,
and role memory. Mutable organisation/project instructions remain outside the
runtime image.

Role memory is DB-backed and source-linked. The document library remains the
canonical project memory, while each role instance can keep a concise cache of
facts, preferences, prior handoffs, and recurring risks. Agents update memory
through `memory.propose_update`, and every memory entry must cite a work item,
document, event, or conversation source.

## Agent Configuration And Lifecycle

The runtime image must not contain mutable organisation, project, or role
configuration. Before starting or waking a role instance, the runtime
materialises an external agent config folder containing:

- `role.md`
- `organisation.md`
- `project.md`
- `tools.md`
- `raci.json`
- `container.json`

Project config supplies role template ids, instance counts, worker/auth
settings, role instructions, write paths, and connector identities. The runtime
uses those values to prepare role containers; it must not bake them into the
image.

The same mounted folder is used to build the role-service configuration:
prompt component paths, memory database path, broker stream, and durable
consumer name are derived from project id, role id, and instance id.

Role containers mount source, organisation config, project config, agent config,
runtime state, and document-library roots at stable paths. Hibernation planning
uses heartbeat, active work, inbox depth, and minimum warm-pool policy; it does
not make work decisions.

Project Manager sweeps are read-only health inspections over work-item state.
They flag blocked, waiting, recovering, and stale non-terminal work so the
Project Manager agent can chase blockers or coordinate handoffs through normal
tools. The sweep service does not auto-close, auto-reopen, or silently advance
work.

## Governance

Every work item carries a governance context: accountable role, responsible
roles, consulted roles, informed roles, sponsor decision points, and required
evidence before handoff, release, or closure.

Consultation is mandatory for RACI `C` roles unless the accountable role records
a governance exception. Informed roles receive concise notices when state
changes, major decisions happen, blockers appear, or releases occur.

Sponsor consultation is required for material changes to scope, priority,
acceptance criteria, user-visible behavior, release risk, cost, compliance,
security posture, or delivery commitment.

Tool authority is role-scoped. All roles can communicate, consult, hand off,
record governance evidence, update document indexes, and report status.
Product Manager and Project Manager can shape backlog/work-item state. Release
Manager owns release/deploy tools. Roles can request out-of-flow help through
consult/handoff tools, but cannot silently execute another role's specialist
authority.

Work-item state is explicit and validated. V3 states are `queued`, `shaping`,
`ready`, `active`, `waiting_human`, `waiting_agent`, `waiting_external`,
`blocked`, `recovering`, `release_review`, `deploying`, `released`, `closed`,
`canceled`, `superseded`, and `failed_terminal`.

## Reporting

Initial reporting routes are:

- `/status`
- `/agents`
- `/work-item/{work-item-id}`
- `/artifact-viewer/{work-item-id}/{artifact-filename}`

The report plane reads projections and document-library metadata. It does not
own lifecycle decisions.

V3 observability starts with a shared telemetry facade configured from
environment variables. Safe-output tool calls emit OpenTelemetry spans,
structured log events, and counters tagged by role instance, tool name, and
success/failure status. Runtime services should use this facade rather than
calling OpenTelemetry SDK objects directly.

Local command smoke:

```powershell
agentic-mesh-v3 --db .tmp/v3.sqlite3 init-db
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev demo-slice --document-library-root .tmp/v3-documents
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev local-e2e-dogfood --document-library-root .tmp/v3-documents
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev status-json
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev serve --document-library-root .tmp/v3-documents
agentic-mesh-v3 --db .tmp/v3.sqlite3 run-tool-mcp-stdio
```

## Adapter Status

- Broker: V3 defines the broker port, local contract adapter, and NATS
  JetStream adapter/factory.
- Agent tools: V3 exposes tool calls through CLI and MCP-compatible JSON-RPC
  stdio so Codex, other workers, and external automation can use the same
  approved pathway.
- Teams: V3 defines connector-neutral inbound messages, local Teams-shaped
  routing, and Graph-backed outbound message delivery with injectable transport.
  Project-channel messages are retained as shared `project.context`.
  Mentioned-role channel messages also route to the mentioned role. Unmentioned
  project-channel messages can fan out to role relevance-check inbox subjects
  so agents decide whether they have something material to add. Bot Framework
  activity ingress is normalised before routing so live Teams DMs, channel
  posts, real mentions, and thread references enter the same connector-neutral
  path.
- OneDrive: V3 defines the document-library port and Graph-backed OneDrive
  adapter with injectable transport. Work-item files live under
  `/documents/work-items/{work_item_id}`.
- Deployment: V3 defines command and no-deployment deployment targets so the
  Release Manager can execute a configured deployment or record a clear
  no-deployment disposition. Release closure is a Release Manager tool action
  and requires a recorded deployment or explicit no-deployment disposition
  before the work item can be moved to released/closed.

## Artifact Library

Work-item evidence lives under:

```text
/documents/work-items/{work_item_id}/
```

Every work item has an `index.md`. The root work-item index is:

```text
/documents/work-items/index.md
```

Durable architecture, product, engineering, QA, release, risk, decision, and
operations documents also live in the document library.
