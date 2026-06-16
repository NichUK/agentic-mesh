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
