# Project Memory

This file records the context needed to resume Agentic Mesh in a fresh chat
after opening `C:\Dev\agentic-mesh` as the workspace.

## 2026-07-17 - V5 Clean Runtime Boundary

- V4 remains the deployed baseline while the ordered `AMV5-*` backlog builds
  V5 as a clean replacement.
- V5 code lives under `src/agentic_mesh_v5` and automated checks reject imports
  from V2, V3, or V4 runtime packages.
- The unversioned CLI continues to target V4 until qualified cutover; V5 uses
  the explicit `agentic-mesh-v5` entry point during development.
- V4 reuse is governed by
  `docs/architecture/v5/v4-asset-inventory.yaml`; port and rewrite decisions
  create new V5 assets and never authorize importing the V4 package.
- Quantauma is retired from active source, configuration, examples, runtime
  terminology, and fixtures. Historical documents remain available; neutral
  `example-project` fixtures preserve isolation coverage.
- V5 organization configuration packages live in the private, secret-free
  `NichUK/agentic-mesh-config` repository and mount at `/mesh/config`; the
  runtime repository stores only its external reference and mount contract.
- V5 resolves exact external package references and dependencies with fixed
  precedence and project overrides last. Effective configuration records
  ordered text, merged JSON, canonical content provenance, and one reproducible
  digest; missing packages, cycles, malformed content, and path escapes fail
  closed.
- Validated V5 configuration can be stored as a digest-addressed immutable
  release in the external repository. One atomically replaced, Git-visible
  activation document keeps the active digest and audit history together;
  expected-current checks serialize competing promotions and rollback only
  repoints to an existing release.
- External SDLC role/flow packages, simplicity and ambiguity guardrails, and
  the extensible tool-profile registry are active in the organization config
  repository. General, development, QA, operations, UX, and independently
  launched recovery images are project-neutral and contain no persona, project
  state, memory, prompt, or credential.
- V5 durable control-plane state uses the dedicated `agentic_mesh_v5` Postgres
  schema. Packaged, checksum-protected SQL migrations run under one transaction
  and advisory lock; composite project-qualified foreign keys prevent
  cross-project references. `agentic-mesh-v5 database-migrate` and
  `database-status` read the database URL only from
  `AGENTIC_MESH_V5_DATABASE_URL`.
- V5 significant transitions use an append-only Postgres event journal and a
  transactional outbox. Source SQL, the correlated project/work-item event,
  and outbound rows share one explicit transaction. Dispatch is at least once:
  `FOR UPDATE SKIP LOCKED` prevents concurrent delivery, while a stable
  idempotency key lets downstream adapters deduplicate a retry after a crash.
- The V5 kernel lifecycle keeps work items in `new`, `active`, `gated`,
  `completed`, or `error`, with optimistic versions. Only sponsor decisions
  resume a gate, ownership is preserved across the pause, concurrent decisions
  resolve once, and terminal completion/error remain distinct and immutable.
- V5 role queues are project/role qualified and use Postgres row locks plus one
  active-lease constraint. Claims carry opaque tokens, heartbeat/completion and
  release require that token, and the next claim transaction reclaims expired
  work using database time. Queue readiness, delay, lease, age, and attempts are
  queryable from durable state.
- The V5 FastAPI control boundary exposes implemented project, work, gate,
  approval, and queue operations under `/api/v1`, plus authorized reads for
  existing agent, handoff, progress, package, and audit records. Usage and
  recovery remain explicitly planned rather than simulated. Bootstrap bearer
  identities come from an external hash-only principals file, every project
  route checks project and scope, and failures use structured problem details.
- The bootstrap CLI stays a thin HTTP client: `control-status` and
  `control-call` cover current `/api/v1` contracts using an externally mounted
  bearer-token file. It requires HTTPS except on loopback, confirms each API
  action ID, uses stable exit codes, and redacts secret-shaped response fields.
- V5 live reads replay same-transaction, append-only projection events into
  project/domain/entity models. Safe project, work, queue metrics, role,
  instance, and structured-progress updates stream through project-authorized
  SSE with global IDs and `Last-Event-ID`; slow clients hold no database lease.
- V5 control requests preserve W3C trace context and record route-template,
  request, project, work-item, queue, correlation, outcome, and bounded metric
  attributes through one OpenTelemetry wrapper. Liveness is database
  independent; readiness reports current/pending/unavailable Postgres state
  with stable redacted reason codes. OTLP export is enabled only through
  external standard endpoint settings and startup does not require a collector.
- V5 native Postgres backup enters a durable database-level write pause that
  waits for existing writers, rejects new mutations, and keeps reads available.
  Custom-format archives have a secret-free checksum/count manifest; restore
  accepts only an empty target, verifies migration and every V5 table count,
  and remains paused until an explicit operator resume. Operations and recovery
  images include `pg_dump`, `pg_restore`, and `psql`; database credentials stay
  in child-process environment rather than arguments, output, or manifests.
- V5 worker execution has provider-neutral engine, thread, active-turn, event,
  completion, usage, policy, and safe-error contracts. The Codex implementation
  uses the official version-pinned `openai-codex` Python SDK and one local stdio
  app-server per opened engine. Codex notification models and exception text do
  not cross the adapter; overload/transport failures are retryable, interruption
  is distinct from failure, credentials remain external, and close is
  deterministic and retryable after a cleanup failure.

## 2026-07-15 - Bounded Missing-Output Repair

- When a V4 role completes a turn without the safe-output required by its
  completion contract, the runtime queues one corrective turn to the same role
  and existing Codex thread with the exact missing predicates.
- If that single repair also omits its durable output, the runtime escalates to
  Project Manager with `completion_repair_exhausted` evidence. It never creates
  an unbounded repair loop.
- This prevents routine omitted handoffs from immediately stranding active work
  in management recovery while preserving role ownership of specialist
  decisions and durable outputs.

## 2026-07-15 - Shared Git And Human-Readable Output Contract

- Every materialized V4 role prompt now includes shared Git best practices:
  inspect repository state before editing, preserve unrelated work, use scoped
  branches and focused verified commits, push recoverable work, integrate by
  reviewed pull request, and record real Git/GitHub evidence.
- Human-facing role output must lead with the outcome and use short paragraphs,
  headings, lists, and whitespace where useful. Unbroken walls of text and raw
  internal/tool-output dumps are explicitly prohibited.
- The common role-agent image includes GitHub CLI so deployments can provide
  project-scoped GitHub authentication without baking credentials into images.

## 2026-06-20 - V4 Remote-Control Runtime Reset

The active implementation direction is now V4 on branch
`codex/v4-remote-control-reset`. V4 replaces the V3 broker/role-service loop
with one Codex app-server remote-control container per configured role
instance. The runtime should be thin infrastructure: Teams/API ingress,
Postgres-backed message queues, Codex app-server WebSocket delivery,
safe-output tools for durable workflow effects, dashboard/reporting,
hibernation/wake coordination, and telemetry.

Important V4 decisions:

- The full SDLC starter team is configured from the start, including Project
  Manager, Delivery Manager, Product Manager, Business Analyst, Research
  Analyst, Enterprise Architect, Solution Architect, Security Architect, UX
  Designer, Engineering, QA Engineer, Platform Engineer, Release Manager,
  Technical Writer, and Prompt Engineer.
- Each role instance gets an externally materialized `AGENTS.md` under the
  project runtime state tree and starts Codex app-server from that folder so
  the role identity is visible to Codex without baking prompts into the image.
- Runtime state and delivery queues are Postgres-backed. SQLite is no longer
  supported for active V4 runtime state. There is no active V4
  NATS broker, V3 supervisor, or `run-agent-service` path.
- The V4 dispatcher polls Postgres, wakes the relevant role service through
  Compose when needed, connects over the role's authenticated internal
  WebSocket, and delivers queued messages through Codex thread/turn methods.
- V4 runtime, dispatcher, and watchdog startup now enforce separate mounted
  system and project-workspace paths. Dispatcher delivery waits for a stopped
  role's Codex app-server health endpoint before claiming work, uses bounded
  WebSocket event reads without timing out the underlying turn, and detects
  stale active deliveries from genuine agent events rather than polling noise.
- LinuxCH dogfood releases refresh the reviewed Compose overlay from the
  system source and reject any staged or merged `PYTHONPATH` that imports V4
  control-plane code from the mutable project workspace. This prevents stale
  project deployment files from disabling dispatcher wake and watchdog loops.
- Conversational replies come from Codex streamed output. Durable work effects
  such as handoffs, approvals, artifact updates, release records, and memory
  updates must still be made through safe-output tools.
- Active dogfood config is
  `examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml`, and the
  active deployment compose file is generated as
  `examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.v4.yml`
  and mirrored to `docker-compose.yml`.

## 2026-06-15 - V3 Agent-Owned Runtime Reset

The project started V3 on branch `codex/v3-agent-owned-runtime`. V3 changes the
direction from runtime-owned lifecycle orchestration to self-contained role
agents. The runtime should provide startup, hibernation, broker, connector,
document-library, reporting, config, and telemetry services; agents should own
work progression, governance, consultation, documentation, handoffs, and
confirmations.

Important V3 decisions:

- Project Manager and Delivery Manager are separate active roles.
- Product Manager owns product priority/scope; Project Manager owns queue
  health, sequencing, regular sweeps, governance hygiene, and closure.
- Broker abstraction comes first, with NATS JetStream as the first target and
  RabbitMQ, Redis Streams, Azure Service Bus, Kafka, SQS/SNS, Pub/Sub, and
  local adapters kept behind the same port.
- OneDrive is the first document-library target so Teams can show Shared Files
  under `/documents`; work items live under `/documents/work-items/{id}`.
  Live OneDrive/SharePoint adapters require `AGENTIC_MESH_ONEDRIVE_TOKEN`
  unless a host injects a custom Graph transport, so the CLI fails early
  instead of letting agents believe unauthenticated document writes succeeded.
- Governance instructions must be explicit in prompts: consult RACI `C` roles,
  inform `I` roles, ask stakeholders for material decisions, and record
  exceptions/evidence.
- V3 topology validation keeps source repo, deployed runtime, runtime state,
  organisation config repo, project config repo, and document library as
  separate roots by default. Identical or nested roots require explicit
  `local_dev_override`.

## Current State

Agentic Mesh is active on the V4 remote-control runtime while the ordered V5
backlog is implemented as a clean replacement. V2 and V3 runtime packages,
legacy console scripts, installer scripts, and tests have been removed from the
active source tree. Operational V4 fixes and new V5 work land separately under:

```text
src/agentic_mesh_v4
tests/test_v4_*.py
src/agentic_mesh_v5
tests/test_v5_*.py
```

The active console scripts are:

```text
agentic-mesh = agentic_mesh_v4.cli:main
agentic-mesh-v4 = agentic_mesh_v4.cli:main
agentic-mesh-v5 = agentic_mesh_v5.cli:main
```

Use feature branches from `develop`, then promote through PRs with Copilot
review unless the sponsor explicitly directs an operational exception.

## Dogfood Deployment

The linuxch dogfood deployment runs V4 on the existing status port:

```text
http://linuxch:8100/status
```

The compose stack should contain V4 runtime services such as:

- `agentic-mesh-runtime-1`
- `agentic-mesh-dispatcher-1`
- `agentic-mesh-postgres-1`
- `agentic-mesh-otel-collector-1`

Teams ingress is separate from the private dashboard route. LinuxCH runs
system Nginx on `10.0.0.116:80/443`; public DNS for `am.nixnet.com` resolves to
the router WAN address and ports 80/443 forward to that host. Nginx exposes
only a capability-protected Teams callback beneath `/teams/activity/`, requires
a Bot Framework bearer header, and proxies accepted requests to the V4 runtime
on `127.0.0.1:8100`. The capability is stored outside Git at
`/etc/agentic-mesh/teams-ingress-path-token` and in Azure Bot messaging
endpoints. Do not replace it with an account-less quick tunnel.

The dashboard remains private at the Tailscale HTTPS route and is not exposed
through `am.nixnet.com`.

Runtime state is Postgres-backed. SQLite files are not active V4 runtime state.

## Removed Legacy Runtime Code

The V2 and V3 source packages and regression tests were removed after V4 became
the active runtime. Keep historical V2/V3 architecture and QA documents as
records, but do not add active code, tests, deployment paths, or prompts that
import `agentic_mesh_v2` or `agentic_mesh_v3`.

## Useful Commands

```powershell
pip install -e .[dev]
pytest -q
agentic-mesh-v4 --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml status-json
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml config --quiet
```

Linuxch deploy:

```powershell
ssh nich@linuxch 'cd /home/nich/agentic-mesh && git pull --ff-only && sh scripts/release-linuxch-compose.sh'
```

## Teams Installation Notes

The v2 project installer reconciles both project-team installs and personal
Teams app installs for people listed in `connectors.teams.people`.

For sponsor DMs to work, each role app and gateway app must have:

- an Entra app registration
- a service principal in the tenant
- an Azure Bot Service resource with the Teams channel enabled
- a published organization Teams app package
- a team install for project-channel interactions
- a personal install for each configured sponsor/operator/release approver

The `AM-Agentic Mesh` gateway failed personal installation until the missing
Azure Bot Service resource was created and its Teams channel was enabled:

```powershell
az bot create --resource-group agentic-mesh-dev --name am-agentic-mesh --app-type SingleTenant --appid 30770259-0fd0-47ff-a1c4-781900081411 --tenant-id 564b667c-5b1a-4bbc-bb43-918b0b765a9b --endpoint https://vpn.nixnet.com/api/messages --sku F0 --display-name "AM-Agentic Mesh"
az bot msteams create --resource-group agentic-mesh-dev --name am-agentic-mesh
```

## Remaining Direction

The v2 spine is intentionally small. The next real work is to add v2-native
long-running role workers, connector ingress, human approval handling, and
deployment actions without restoring v1 file-backed queues or v1 result
parsing.

## V3 Worker Adapter Progress

V3 now has a command-backed `codex-cli` worker adapter for explicit
`run-agent-once --worker codex-cli` runs. The adapter wraps the generated role
prompt with the safe-output tool contract, runs `codex exec` or an injected
test command, and treats stdout as an operational JSON envelope listing tools
already called through CLI/MCP. Durable state remains safe-output records, not
free-text or legacy result parsing.

`run-agent-once` now uses the role's configured `worker` block from
`project.yaml` when no `--worker` override is passed. If a role has no worker
configured, it falls back to the echo worker for local smoke testing.

V3 also has `run-agent-service`, a bounded long-running role loop for
containerised role agents. It polls the configured broker inbox, records
agent heartbeat/status into the runtime DB for `/agents`, uses the same
project-config worker selection as `run-agent-once`, and can idle-exit for
hibernation-oriented supervision.

V3 role container specs now generate the `agentic-mesh-v3 run-agent-service`
command, and materialized `container.json` includes that command alongside
mounts and prompt paths. The default mounted project config path is
`/mesh/project/agentic-mesh/project.yaml`.

V3 now has a Compose renderer for role services. It turns materialized
`RoleContainerSpec` values into one Docker Compose service per role instance,
using the generated `run-agent-service` command, stable mounts, environment,
and a configured network name.

The `materialize-agent-configs` CLI command now materializes per-role agent
config folders from `project.yaml`, role templates, organisation/tool
instruction files, and explicit external path mounts. It can also write the
Compose role-services YAML in the same run via `--compose-output`.

V3 safe-output tools are now discoverable through a shared role-scoped tool
catalog. Operators can run `agentic-mesh-v3 tool-catalog --role-id <role>`,
and MCP-native agents can call the read-only `agentic_mesh_v3.tool_catalog`
tool before emitting durable safe-output calls.

Materialized V3 agent `tools.md` files now append the role-scoped safe-output
tool catalog, marking each tool allowed or blocked for that role and noting
terminal tools. This gives Codex and other workers prompt-visible authority
guidance before they call CLI/MCP tools.

V3 role templates now load through a validator before agent config
materialization. Required charter fields include role profile,
accountabilities, decision rights, boundaries, collaboration style, quality
bar, memory focus, workflows, standards references, anti-patterns, and
standing instructions; all starter templates under `config/roles` are covered
by tests.

V3 work-item document indexes now validate evidence quality before writing.
`index.md` records must include more than status metadata, and duplicate
artifact paths, decisions, or risks are rejected so agents cannot publish
repeated prose dumps or status-only placeholders as enterprise evidence.

V3 governance now has an agent-facing checklist helper that compares a work
item's RACI context with recorded consultation, informed-update, exception, and
approval evidence. The helper reports what remains unresolved for the role to
act on; it does not move lifecycle state or make governance decisions.

Role-agent prompt assembly now includes a `<governance-checklist>` whenever a
governance context is supplied, so agents see missing consultations, informed
updates, and sponsor decisions as concrete work requirements before handoff.

The V3 database can now derive a work item's governance context and checklist
from stored work-item governance, governance safe-output records, and approval
responses. This keeps reporting/prompt consumers aligned while preserving the
rule that agents, not the database, decide how to resolve the checklist.

V3 work-item detail pages now render the derived governance checklist, making
missing consultations, informed updates, sponsor decisions, and recorded
exceptions visible without forcing users or agents to inspect raw JSON.

`handoff.require` is now validated as a full requirements packet. Agents must
include work item, target role, phase, accountable role, next action,
acceptance/evidence requirements, artifact links, open decisions/risks,
consulted/informed roles, and stakeholder follow-up, even when some lists are
intentionally empty.

Project Manager sweeps can now publish `project_sweep.finding` messages to the
Project Manager agent inbox. The sweep remains read-only; the Project Manager
agent receives the finding and must use normal tools to chase, unblock,
rescope, hand off, or close work.

The V3 CLI exposes sweep publication through `sweep-project
--publish-to-project-manager` using the configured project broker. This gives
operators and future schedulers a standard route to wake the Project Manager
agent for stale/blocked work without giving the runtime lifecycle authority.

The V3 broker port now includes pending message inspection and dead-letter
operations in addition to publish/fetch/ack/nack/depth. The in-memory adapter
implements these fully for contract tests, and product code should continue to
target the broker interface rather than a specific backend.

V3 role services now enforce a configurable `max_delivery_attempts` limit.
Failed messages are retried until the limit is reached, then moved to broker
dead-letter storage with the failure reason so recovery can inspect them
instead of leaving the agent in an endless retry loop.

Agent status now carries `dead_letter_depth` as well as `inbox_depth`, and the
agents page renders the dead-letter count. The database migration adds the
column for existing V3 state files.

The V3 CLI now exposes read-only broker inspection through `broker-inspect`.
It loads the configured project broker and reports pending and dead-lettered
messages as JSON without claiming or mutating work, giving operators a standard
way to inspect inbox/recovery state.

Work-item document index writes now refresh `/documents/work-items/index.md`
automatically. Agents still own the content of each work-item dossier, but the
root browse index no longer depends on a second manual tool call. Root index
links are relative to `/documents/work-items/`, for example
`work-123/index.md`, so the index renders correctly from OneDrive/Teams Shared
Files and the runtime artifact viewer.

V3 lifecycle planning now supports role-grouped batch decisions. Wake/start
actions are planned before idle hibernation, and warm-pool counts are updated
as decisions are made so the runtime does not hibernate every idle instance of
the same role in one pass.

Role-agent prompts now include explicit broker message metadata and render the
assignment payload as deterministic JSON. This gives agents stable message ids,
subjects, and source context without letting the runtime interpret the work.

V3 status tables now link work-item IDs and queue-linked work items to the
corresponding `/work-item/{work_item_id}` detail route, making the reporting
plane easier for sponsors and operators to traverse.
The `/status` page also separates attention-needed work, stale work, and
agent governance waits from the general active-work list. Work-item status
rows carry `updated_at` and `attention_reason` so stuck work is visible without
running a separate Project Manager sweep first.
V3 `release.deploy` now records failed deployment targets as release rows with
status `failed` and moves the work item to `recovering`, preserving deployment
output in `next_action` for Release Manager/operator follow-up.

Starter role documentation obligations now have concrete durable document
targets, and role validation can check repo-local documentation paths. This
prevents role prompts from sending agents toward missing library files.

The V3 CLI now exposes `lifecycle-plan`, a read-only operator command that
uses the DB agent status projection and role-grouped lifecycle planner to show
start, wake, hibernate, and no-op decisions without mutating container state.

V3 safe-output tools now include `artifact.link` for role-owned evidence
linking. Any role can register an existing document-library path as a work-item
artifact, and the tool rejects paths that escape the document library.

Prompt Engineer remains a specialist consultation role, not a default
dependency for every development phase. V3 development RACI excludes it by
default, while governance instructions require consultation when prompt
components, role instructions, safe-output guidance, context loading, memory
instructions, or prompt-driven behaviour are in scope.

V3 work-item indexes now include first-class sections for consultations,
approvals, and explicit evidence in addition to artifacts, decisions, risks,
and next action. The document-library validation rejects duplicate entries in
those named sections so dossiers stay factual rather than repetitive.

V3 project config now loads `release_deployment_targets` and the CLI safe-output
tool path passes them into `V3ToolService`. Release Manager `release.deploy`
can therefore execute configured command targets or explicit no-deployment
targets from `project.yaml`.

The V3 MCP stdio runner now uses the same project-config-backed `V3ToolService`
wiring as CLI `tool-call`, including document-library and release deployment
target adapters.

V3 role services now use runtime database-backed role memory. Safe-output
`memory.propose_update` records are loaded into future role-agent prompts for
the same role instance, and agent-run observations are recorded through the
same runtime DB memory path. The standalone SQLite memory adapter remains
available for local adapter use and focused tests.

V3 governance safe-output tools now publish target-role inbox messages when a
broker is configured. `handoff.require`, `consult.request`, and
`informed.update` still record governance evidence, but they can also enqueue a
connector-neutral message on `agent.{target_role}` so agents, not the runtime,
drive follow-on work.

V3 Teams ingress now records inbound connector-neutral messages into the
runtime conversation read model, and role-agent prompts load recent messages
for the current `conversation_ref`. This keeps DMs and project-channel
conversation context available across stateless worker executions.

V3 work-item terminal states now sync linked queue/backlog item status in the
runtime read model. When linked work reaches `closed`, `canceled`,
`superseded`, or `failed_terminal`, the queue item leaves current backlog views
without requiring a second explicit backlog update.

V3 approval responses recorded through the CLI now wake the requesting role
when project broker config is supplied. The runtime records the approval
decision, then publishes an `approval.response_recorded` message to
`agent.{requested_by_role}` so the role agent can continue the slice.

V3 CLI and MCP safe-output tool services now build stakeholder outbound
bridges from `project.yaml` Teams connector settings. Local/test projects can
use `connectors.teams.adapter: local`; Graph/Teams bot connector adapters
require `AGENTIC_MESH_TEAMS_TOKEN` so targeted sponsor or stakeholder messages
cannot be reported as delivered without a configured connector credential.

The V3 project schema now matches the parsed runtime config surface for local
broker defaults, document-library defaults, role worker/auth settings, Teams
connector adapter settings, and release deployment targets. It validates the
current dogfood project config while staying permissive for broader
organisation/project fields that V3 has not interpreted yet.

V3 project config now parses the `flow` block. Agent config materialization
uses an explicit `--flow-config` when provided, otherwise it resolves the
project-selected flow template/path from `project.yaml`; `flow.template: sdlc`
maps to `config/flows/sdlc-v3.yaml`. The mounted `raci.json` therefore follows
the project-selected flow instead of silently falling back to the in-code
starter SDLC matrix.

V3 role services now load database-derived governance context/checklists for
inbox messages that carry `work_item_id`. `run-agent-once` and
`run-agent-service` pass this provider automatically, so agent prompts include
current missing consultations, informed updates, and sponsor decisions without
manual caller injection.

V3 role services now consume both direct `agent.{role}` and role relevance
`agent.{role}.relevance` inbox messages. Direct work is processed first, then
project-channel relevance checks, so unmentioned project-channel posts can be
reviewed by roles without broadening consumers to every stream subject.

V3 agent config materialization now writes a mounted `system.md` prompt
component. `materialize-agent-configs --system-instructions-file ...` can
override it; otherwise the default comes from
`config/prompts/worker/system-security.xml` plus
`config/prompts/worker/instructions.xml`. Default tool guidance now comes from
`config/prompts/worker/safe-outputs.xml` before the role-scoped tool catalog is
appended. This keeps shared agent behavior in prompt config rather than hidden
inside the runtime image or code path.

V3 lifecycle now has an explicit Compose actuator path. `lifecycle-plan` remains
read-only; `lifecycle-apply` converts actionable start/wake/hibernate decisions
into Docker Compose commands, dry-runs by default, and only executes container
starts/stops when `--execute` is supplied. Compose rendering and lifecycle
execution share the same role-instance-to-service-name mapping.

V3 agent reporting now surfaces database-derived role-memory health. The
`/agents` page and `status-json` projection include memory entry count and the
last memory timestamp for each role instance, derived from `role_memory` rather
than heartbeat payloads.

V3 artifact rendering now preserves Mermaid fenced code blocks as controlled
Mermaid render blocks while continuing to sanitize artifact content with
`bleach`. The viewer only loads Mermaid when an artifact contains a Mermaid
fence.

V3 `work_item.upsert` now materializes minimum governance context for
safe-output-created work items when agents omit it. The tool fills phase,
accountable role, and responsible role from the work item state/owner while
preserving richer RACI fields supplied by the agent.

V3 in-memory broker subject matching now follows NATS-style wildcard semantics
for local contract tests: `*` matches one subject token and `>` matches the
remaining tail when used at the end of a pattern.

V3 role services now require each processed inbox message to report at least
one terminal safe-output signal (`status.reply`, `status.complete`, `noop`, or
`report.incomplete`) before the message is acknowledged. Non-terminal-only
worker runs are retried or dead-lettered.

V3 role-service status now reports role-specific inbox depth by summing the
role's direct and relevance consumers. It no longer reports whole-stream depth,
so `/agents` and hibernation planning do not treat unrelated role messages as
this agent's backlog.

V3 command deployment targets now convert subprocess timeouts into explicit
failed `DeploymentResult` values. `release.deploy` therefore records timeout
failures as release evidence and moves the work item into `recovering` instead
of losing the failure as an uncaught runtime exception.

V3 release deployment state is now visible in the work-item lifecycle:
`release.deploy` moves work into `deploying` while the configured target runs
and moves successful deployment or no-deployment dispositions to `released`.
`release.close` then performs the separate final `closed` transition, including
when the item is already in `released`.

The role-charter documentation now states explicitly that Prompt Engineer and
Project Manager are peer roles: Project Manager owns project control and may
consult Prompt Engineer for prompt/tool/agent-behaviour blockers, while Prompt
Engineer owns prompt-contract specialist advice without taking over project
sequencing.

V3 role-service automatic run observations now record source-linked memory
provenance. The service cites the work item, conversation, source message, or
broker message that produced the observation instead of using a generic
`agent-run` source.

V3 work-item index artifact links now render relative to the work-item folder
unless an explicit backend URL is present. Same-folder artifacts link as
`020-product-definition.md`, sibling work items as `../work-id/index.md`, and
root-level durable docs via `../../...`, so Teams/OneDrive Shared Files remain
browsable without runtime URL rewriting.

V3 Graph-backed Teams outbound delivery now sanitizes Markdown-rendered HTML
before posting. Script/style blocks, unsafe attributes, and unsupported HTML are
stripped while normal Markdown formatting is preserved for stakeholder-facing
agent replies, approvals, and questions.

V3 agent config materialization now validates topology before writing role
instance folders or Compose output. Operators must provide a distinct
`--deployed-runtime` path, and source/runtime/project/state/document path
collapse is rejected unless an explicit local-dev override is supplied.

V3 Project Manager sweep findings now include the work-item status URL and
artifact count in the broker payload. The runtime still performs a read-only
scan; the Project Manager agent receives enough context to chase, unblock, or
close work through normal tools.

V3 lifecycle apply now records planned or executed Compose lifecycle actions
into the event log. Executed successes update the agent container-state
projection to `running` or `hibernated`; executed failures show as
`lifecycle_failed` with the failure detail on `/agents`.

V3 governance communication tools now validate minimum useful payloads before
recording evidence: consults require a target role and question, informed
updates require a target role and message, stakeholder questions require a
question, and governance exceptions require a reason.

V3 DB-backed role-service runs now audit terminal safe-output calls against the
runtime tool-call table. The worker's returned operational envelope is no
longer enough on its own; the run must also record a new terminal tool call
through the approved safe-output service before the inbox message is acked.

V3 tool catalog entries now expose required payload fields through CLI/MCP
tool discovery, and materialized agent `tools.md` files render those required
fields beside each role-scoped tool. This keeps prompt context aligned with
safe-output validation.

V3 tool contract metadata is now shared by catalog discovery and runtime
safe-output enforcement. `V3ToolService` validates required payload fields
before recording a durable tool call, so incomplete tool attempts fail without
leaving misleading call records.

V3 governance prompt instructions now name the safe-output tools agents should
use for each governance duty: `consult.request`, `informed.update`,
`stakeholder.ask_question`, `governance.record_exception`, and
`document.write_work_item_index`. This makes consultation and evidence capture
less likely to become prose-only claims.

V3 `status.update` now has a visible work-item effect. It can still be used as
an audit-only non-terminal progress call without `work_item_id`, but when a
work item is supplied it updates that item's next action/current phase without
performing a lifecycle state transition.

V3 approval responses now hand visible ownership back to the requesting agent
when the work item was in `waiting_human`. The DB records the approval response
and moves the work item to `waiting_agent` with a next action for the requesting
role; the role agent remains responsible for deciding the next lifecycle step.

V3 `approval.request` now projects the human wait into the work-item read
model. The tool records the approval request and moves existing work to
`waiting_human` with sponsor ownership/next action in the same transaction, so
status pages show that the sponsor is the current blocker.

V3 `handoff.require` now projects visible work ownership to the target role.
The tool still records governance evidence and publishes the target-role inbox
message, but it also moves the work item to `waiting_agent`, sets the owner to
the target role, and uses the handoff's required next action on status pages.

V3 Project Manager sweeps now surface unresolved governance checklists on
active work. Missing consultations, informed updates, and sponsor decisions
become `project_sweep.finding` reasons before the work merely ages into a stale
item, while explicit blocked/waiting states still take priority.

The V3 `/status` read model now uses the same unresolved-governance signal for
active work-item attention reasons. Sponsors and operators can see missing
consultations, informed updates, or sponsor decisions directly on `/status`
without waiting for a Project Manager sweep or stale-work timer.

V3 conversation recording now preserves mentioned-role metadata from Teams
activities. Recent conversation context in role prompts includes the mentioned
roles, helping agents distinguish shared project-channel context from messages
aimed at a specific specialist.

V3 project config now exposes target repositories to role containers. Explicit
`target_repositories` entries, or legacy `workspace.repositories` entries, are
resolved relative to `project.yaml` and materialized as mounts under
`/mesh/workspaces/{repository_id}` so agents have project-granted source access
without confusing the runtime source mount at `/mesh/source`.

V3 topology validation now includes project target repositories. Target repos
may match the source repo for dogfood work, but they cannot overlap deployed
runtime, runtime state, organisation config, project config, document-library
roots, or each other unless `local_dev_override` is set.

V3 terminal work can now be restored only through the explicit
`work_item.reopen` safe-output tool. Normal `work_item.update_state` still
rejects terminal-to-active transitions. Product Manager, Project Manager, and
Release Manager have reopen authority; the tool requires a reason, records a
`work_item.reopened` event, syncs linked queue/backlog state, and moves the item
to a controlled non-terminal state such as `shaping`, `active`, `recovering`,
or `release_review`.

## 2026-07-10 TOGAF-Aligned Enterprise Architecture Upgrade

- V4 Product Definition records structured architecture impact as `none`,
  `material`, or `uncertain`; Product Definition cannot complete while impact
  remains unassessed.
- Material or uncertain work routes through Enterprise Alignment and requires
  Enterprise Architect conformance approval or a sponsor-approved exception
  before implementation planning and release.
- Enterprise Architect now owns the durable project architecture portfolio at
  `/documents/020-architecture/enterprise/`, including the target operating
  model, Business/Data/Application/Technology views, requirements, gaps,
  transitions, roadmap, conformance, exceptions, and change log.
- V4 prompt materialisation includes full core workflows, standards,
  project-specific instructions, document accountabilities, and resolved
  RACI/consult/gate/handoff guidance.
- Ordered project documents use three-digit prefixes. Slice-specific evidence
  remains in the work-item dossier; durable architecture knowledge belongs in
  the portfolio.

## 2026-07-13 V4 Human Message Wake Recovery

- A stopped Project Manager was not woken because runtime, dispatcher, and
  watchdog had been recreated against the repository example project instead
  of the external project root. LinuxCH control-plane services must mount
  `/home/nich/agentic-mesh-projects/agentic-mesh-dev` and import runtime code
  only from `/mesh/system/src`.
- Human Teams/API/CLI conversation messages take precedence over internal
  safe-output and runtime-escalation backlog for the same role. Explicit live
  steering remains highest priority.
- Codex `turn/steer` requires `expectedTurnId`. A steering message that cannot
  reach a live turn is downgraded and delivered as a normal queued turn rather
  than failing or remaining stranded.
- LinuxCH release cleanup removes stopped role containers before starting the
  dispatcher, so automatic queue-driven wake cannot race container removal.

## 2026-07-14 V4 Review Proportionality And Convergence

- A dispatch-incident work item accumulated a disproportionate twelve-revision
  speculative security-design loop and 92 document revisions. The sponsor
  stopped the active Security turn; Project Manager durably cancelled the
  handoff and restored ownership to bounded triage of the original incident.
- Shared V4 standing instructions now require every consultation, revision, and
  handoff to directly advance the accepted outcome or resolve a material
  blocker. Minor, speculative, theoretical, and implementation-detail findings
  are non-blocking follow-up unless their concrete current impact is stated.
- The same material finding may be returned for up to three focused
  correction-and-re-review loops. Each loop must stay tied to the overall
  outcome and use the minimum engineering necessary. After the third failed
  correction, the loop stops for deeper-problem disposition or sponsor
  escalation; a fourth specialist bounce requires explicit sponsor direction.
- Out-of-scope discoveries require a separately prioritized work item, sponsor
  stop instructions terminate the review chain, and human-facing responses
  must not contain raw session JSONL or unfiltered tool/search transcripts.

## 2026-07-15 V4 Unattended Elicitation Handling

- Unattended role turns use `approval_policy=never` and have no interactive UI
  capable of answering Codex `mcpServer/elicitation/request` server requests.
  Leaving those requests unanswered causes the same request to replay after
  every WebSocket read timeout and indefinitely blocks the active turn.
- The runtime declines optional plugin-install suggestions and cancels other
  interactive MCP elicitations for unattended roles. Ordinary command, file,
  and permission approvals retain their existing automatic acceptance.
- Agents must use already-configured tooling such as authenticated `gh` before
  suggesting optional plugins, and must route genuine human questions through
  normal conversation or the durable sponsor-decision path.

## 2026-07-16 V4 Sponsor Notification Gate

- A Project Manager moved the shared-fleet migration to `blocked_on_human`
  without creating or delivering a sponsor decision, leaving every agent idle
  while the sponsor had no notification.
- Human-wait states now require an open Sponsor decision requested by the same
  blocking role and a successful Teams delivery receipt with an activity id.
- A dashboard-only, pending, or failed notification cannot satisfy the gate;
  the work item remains unchanged and the delivery failure must stay visible.
- Turn completion also enforces nonterminal continuity: tracked work must be
  genuinely terminal, have a queued/active durable handoff, or have a
  successfully delivered Sponsor decision. Milestone states such as
  `stage1_complete` cannot silently end the overall job.
