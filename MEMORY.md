# Project Memory

This file records the context needed to resume Agentic Mesh in a fresh chat
after opening `C:\Dev\agentic-mesh` as the workspace.

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
