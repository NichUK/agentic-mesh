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
- Governance instructions must be explicit in prompts: consult RACI `C` roles,
  inform `I` roles, ask stakeholders for material decisions, and record
  exceptions/evidence.

## Current State

Agentic Mesh is on the v2 runtime reset branch:

```text
codex/v2-runtime-reset
```

The v1 Python package and v1 tests have been removed. Active implementation
lives under:

```text
src/agentic_mesh_v2
tests/test_v2_*.py
```

The installed console script is:

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
root browse index no longer depends on a second manual tool call.

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
