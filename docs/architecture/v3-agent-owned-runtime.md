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
image. Role templates are validated before materialization so placeholder
purpose-only roles cannot be mounted as executable agent identities.

The same mounted folder is used to build the role-service configuration:
prompt component paths, memory database path, broker stream, and durable
consumer name are derived from project id, role id, and instance id.
At startup the runtime materializes one folder and one role-service
configuration per configured role instance. `container.json` includes the
service command for the role container, which runs `agentic-mesh-v3
run-agent-service` against mounted `/mesh/agent`, `/mesh/state`, and
`/mesh/project/agentic-mesh/project.yaml` paths.

Role containers mount source, organisation config, project config, agent config,
runtime state, and document-library roots at stable paths. Hibernation planning
uses heartbeat, active work, inbox depth, and minimum warm-pool policy; it does
not make work decisions.
The V3 Compose renderer converts materialized role container specs into one
Compose service per role instance, using the generated `run-agent-service`
command and the same mounted path contract.

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

Governance checklists compare a work item's RACI context with recorded
consultation, informed-update, exception, and approval evidence. They are
agent-facing guidance: they tell the accountable role what must still be
resolved before handoff or phase closure, but they do not advance state or let
the runtime make the decision.
When a role service receives a governance context, prompt assembly includes the
matching checklist so the agent sees concrete missing consults, informed
updates, and sponsor decisions alongside the raw RACI context.
The runtime database can derive the same checklist from the work-item read
model, recorded governance safe-output calls, and approval records. This keeps
dashboards and prompt builders aligned without giving the database authority to
close or advance work.

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
agentic-mesh-v3 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml run-agent-once --role-id product-manager --agent-config-dir .tmp/v3-agents/product-manager/1 --runtime-state-dir .tmp/v3-state
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml run-agent-service --role-id product-manager --agent-config-dir .tmp/v3-agents/product-manager/1 --runtime-state-dir .tmp/v3-state --idle-exit-seconds 300
agentic-mesh-v3 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml materialize-agent-configs --image agentic-mesh-v3:local --source-repo . --organisation-config-repo ../org --project-config-repo examples/projects/agentic-mesh-dev --agent-config-root .tmp/v3-agents --runtime-state-dir .tmp/v3-state --document-library-root .tmp/v3-documents --role-templates-dir config/roles --compose-output .tmp/v3-compose/roles.yml
```

## Adapter Status

- Broker: V3 defines the broker port, local contract adapter, and NATS
  JetStream adapter/factory.
- Agent tools: V3 exposes tool calls through CLI and MCP-compatible JSON-RPC
  stdio so Codex, other workers, and external automation can use the same
  approved pathway. Tool discovery is available through `tool-catalog
  --role-id ...` and the read-only `agentic_mesh_v3.tool_catalog` MCP tool, so
  agents can inspect their role-scoped permissions instead of guessing tool
  names. Stakeholder-facing delivery tools call the configured bridge and fail
  when no bridge is configured; agents must not receive successful tool results
  for messages that were not handed to a connector.
  `status.reply` remains valid as an audit-only terminal record, but when it
  carries target metadata it sends the Markdown reply through the bridge.
  `approval.request` records the approval request and, when target metadata is
  present, sends the sponsor-facing approval request through the bridge.
  `stakeholder.ask_question` records governance evidence and, when target
  metadata is present, sends the question through the bridge.
  Materialized agent `tools.md` files include the same role-scoped catalog so
  each worker sees allowed and blocked tool authority in its prompt context.
- Worker adapters: V3 provides an echo worker for local smoke tests, a
  safe-output subprocess adapter for external agents, and a Codex CLI adapter.
  The Codex adapter wraps the generated role prompt with the V3 safe-output
  contract before running `codex exec`, then expects only a small operational
  JSON envelope listing tool calls already made through CLI/MCP. Durable state
  still comes from safe-output tools. `run-agent-once` uses the role worker
  configured in `project.yaml` when no worker override is supplied, falling
  back to the echo worker only for unconfigured local smoke runs.
- Role service loop: `run-agent-service` keeps a configured role instance
  alive, polls its broker inbox, records heartbeat/status into the reporting
  database, and can exit after an idle window so the runtime can hibernate the
  container without losing role identity or memory.
- Teams: V3 defines connector-neutral inbound messages, local Teams-shaped
  routing, and Graph-backed outbound message delivery with injectable transport.
  Project-channel messages are retained as shared `project.context`.
  Mentioned-role channel messages also route to the mentioned role. Unmentioned
  project-channel messages can fan out to role relevance-check inbox subjects
  so agents decide whether they have something material to add. Bot Framework
  activity ingress is normalised by the Teams activity router and then handed
  to the bridge, so live Teams DMs, channel posts, real mentions, and thread
  references enter the same connector-neutral broker path. The local status
  server exposes `/teams/activity` as the first HTTP ingress hook for this path;
  `serve --project-config ...` wires it to the configured broker and role
  identities.
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

Work-item indexes are evidence records, not status placeholders. The document
library writer rejects status-only `index.md` records and duplicate evidence
links or repeated decision/risk entries, so agents must record useful next
actions, artifacts, decisions, risks, or other concrete evidence before
publishing the work-item index.

Durable architecture, product, engineering, QA, release, risk, decision, and
operations documents also live in the document library.
