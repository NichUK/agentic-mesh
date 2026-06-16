# Project Memory

This file records the context needed to resume Agentic Mesh in a fresh chat
after opening `C:\Dev\agentic-mesh` as the workspace.

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

Agentic Mesh is now progressing through stacked V3 implementation branches.
The V2 console entry point remains present for compatibility while V3 is
proved, but new runtime-reset work should land under the V3 package:

```text
src/agentic_mesh_v3
tests/test_v3_*.py
```

The installed V3 console script is:

```text
agentic-mesh-v3 = agentic_mesh_v3.cli:main
```

Use feature branches stacked from the latest V3 PR branch, then promote through
PRs with Copilot review. Do not restart broad V1/V2 dogfood work unless the
sponsor explicitly asks for an operational fix.

The legacy V2 console script still exists as:

```text
agentic-mesh = agentic_mesh_v2.cli:main
```

## Dogfood Deployment

The linuxch dogfood deployment runs v2 only on the existing port:

```text
http://linuxch:8100/status
```

The compose stack should contain only:

- `agentic-mesh-v2-runtime-1`
- `agentic-mesh-otel-collector-1`
- external `agentic-mesh-cloudflared`

Old v1 runtime/project output on linuxch was backed up to:

```text
/home/nich/agentic-mesh-v1-backups/v1-cutover-20260612T080527Z.tar.gz
```

Secrets and worker credential homes were left in place.

## V2 Runtime Shape

V2 currently provides:

- SQLite runtime database
- explicit work-item state machine
- safe-output service and role-scoped tool policy
- role-service run wrapper with terminal safe-output enforcement
- TOGAF-aligned document framework primitives
- release service requiring deployment or no-deployment disposition before
  closure
- v2 HTTP status/reporting server
- v2 CLI commands: `init-db`, `demo-slice`, `status-json`,
  `validate-topology`, and `serve`

The live v2 smoke slice is:

```text
work-v2-demo-slice
```

It proves queue capture, work item promotion, product/engineering/QA/release
role runs, safe-output recording, artifact records, release evidence, and
closed work-item state.

## Useful Commands

```powershell
pip install -e .[dev]
pytest -q
agentic-mesh --db .tmp/v2.sqlite3 init-db
agentic-mesh --db .tmp/v2.sqlite3 demo-slice
agentic-mesh --db .tmp/v2.sqlite3 status-json
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml config --quiet
```

Linuxch deploy:

```powershell
ssh nich@linuxch 'cd /home/nich/agentic-mesh && git pull --ff-only origin codex/v2-runtime-reset && sh scripts/release-linuxch-compose.sh'
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
