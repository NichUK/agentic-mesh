# Linuxch Docker Deployment

Status: draft dogfood deployment note

Date: 2026-06-02

## Target Topology

Agentic Mesh dogfood containers should run on the `linuxch` VM:

- SSH user/host: `nich@10.0.0.65`
- Parent host: `nichserv`
- Parent host address: `10.0.0.4`
- External ingress name: `vpn.nixnet.com`

The Docker network and all role-agent containers run on `linuxch`. The external
Teams bot ingress path should route to the listener container on `linuxch`.

Assumed public bot endpoint:

```text
https://vpn.nixnet.com/api/messages
```

Internal listener:

```text
0.0.0.0:3978/api/messages
```

## Current Deployment Status

Deployment was started on `linuxch` under:

```text
/home/nich/agentic-mesh
```

The Compose stack is running with:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml up -d
```

Verified from the VM and local network:

- `http://127.0.0.1:3978/healthz`
- `http://10.0.0.65:3978/healthz`

Not yet verified externally:

- `https://vpn.nixnet.com/healthz`
- `https://vpn.nixnet.com/api/messages`

Ports `80`, `443`, and `3978` on `vpn.nixnet.com` refused connections from the
Codex machine during the latest test. A temporary Cloudflare quick tunnel was
used for public HTTPS verification instead:

```text
https://tradition-matched-scientists-functions.trycloudflare.com
```

The Azure Bot resources were temporarily updated to the tunnel endpoint. The
permanent public route must be configured before those endpoints are moved to
`vpn.nixnet.com`.

Latest SDLC/Teams smoke evidence is recorded in:

```text
docs/operations/sdlc-teams-smoke-test.md
```

## Compose Profile

Use the base compose file plus the Linux VM overlay:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml up -d
```

Build the local dogfood image only when the Agentic Mesh runtime code changes:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml --profile build-image build runtime-image
```

That build step must not be required for organization or project configuration
changes.

The overlay adds:

- `teams-bot-listener`, exposing host port `3978`
- restart policies for the runtime services
- `AGENTIC_MESH_PUBLIC_BOT_ENDPOINT=https://vpn.nixnet.com/api/messages`

## Runtime Image And Mounted Configuration

Agentic Mesh role, router, control-plane, connector, and bot-listener services
share the same application image in this dogfood profile:

```text
agentic-mesh:local
```

That image contains the Python runtime package only. Organization defaults,
project files, project repositories, runtime state, and secrets are mounted into
the container:

```text
/mesh/system                    # system repository, read-only
/mesh/project                   # examples/projects/agentic-mesh-dev
/mesh/workspaces/agentic-mesh   # dogfood workspace repository
```

Runtime path environment variables make those boundaries explicit:

```text
AGENTIC_MESH_CONFIG_ROOT=/mesh/system
AGENTIC_MESH_PROJECT_FILE=/mesh/project/agentic-mesh/project.yaml
AGENTIC_MESH_WORKSPACE_ROOT=/mesh/workspaces/agentic-mesh
AGENTIC_MESH_STATE_ROOT=/mesh/project/state
```

The project file then declares the workspace/repository shape:

```yaml
workspace:
  root: .
  default_repository: agentic-mesh
  repositories:
    agentic-mesh:
      type: git
      path: .
      default_branch: main
```

Role `write_paths` and flow `artifact_path` values are relative to that project
workspace. This is how role agents get access to real project files such as
`src/**`, `tests/**`, and `docs/**` without those files being baked into the
runtime image.

For production or multi-project deployments, replace `agentic-mesh:local` with
a centrally published image such as `registry.example.com/agentic-mesh/runtime`.
Then mount tenant, organization, and project configuration from external
volumes or repositories without rebuilding the image.

The Linux overlay also switches the Teams connector to role-bot outbound mode:

```powershell
python -m agentic_mesh.cli teams-bot-connector-loop --connector teams --channel all --secret-root /mesh/project/state/secrets --poll-seconds 5
```

Role bot app ids and secrets are copied from Key Vault into ignored runtime
state on the VM:

```text
/home/nich/agentic-mesh/examples/projects/agentic-mesh-dev/state/secrets
```

The base profile still runs:

- `router`
- `control-plane`
- `teams-connector`
- configured role-agent containers
- `otel-collector`

## OpenTelemetry To SigNoz

SigNoz runs separately on `linuxch` on the external Docker network
`observability`. The Agentic Mesh `otel-collector` joins that network in the
Linux overlay and exports logs, metrics, and traces to the external SigNoz
gateway endpoint:

```text
10.0.0.65:4317
```

The gateway should export to:

```text
signoz-otel-collector:4317
```

Application containers should keep `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at the
project collector:

```text
otel-collector:4317
```

Role-agent service names follow the organization naming defaults. The dogfood
Engineering instance emits as:

```text
AM.dev-team.engineering.1
```

## Teams Bot Listener

The listener command is:

```powershell
python -m agentic_mesh.cli teams-bot-listener --host 0.0.0.0 --port 3978
```

Routes:

- `GET /healthz`: health check
- `POST /api/messages`: accepts Teams bot activities
- `GET /admin/config`: returns the currently loaded project, connector,
  channels, roles, and config paths
- `POST /admin/reload-config`: reloads mounted Agentic Mesh config and project
  config without restarting the container

The listener stores raw Teams activities under runtime state and normalizes:

- supported `human_response.submit` Adaptive Card invokes into
  `human_response.received` role queue messages
- normal Teams channel `message` activities from mapped project channels into
  `sponsor_intake.requested` work items at the flow's configured default intake
  state

For the dogfood SDLC flow, a message delivered from `all-agents` becomes a
Business Analyst work item in `business_analysis` with the default sponsor
work item type `spike`.

This depends on Microsoft Teams delivering the channel message to the bot. If
Teams does not send a bot activity for channel-wide mentions such as
`@all-agents`, add a Graph channel read/subscription connector next rather than
manually starting the work item.

## Reloading Listener Configuration

When mounted organization config, flow templates, project config, connector
channel mappings, or role definitions change, reload the listener in place:

```powershell
curl -X POST http://127.0.0.1:3978/admin/reload-config
```

The endpoint rebuilds the listener's project config, connector config, message
store, journal, and secret resolver from the configured paths. It does not
restart the container and it does not interrupt the rest of the Compose stack.

Use a container restart only for runtime code or dependency changes that require
rebuilding `agentic-mesh:local`.

## Azure Bot Endpoint Update

Once `vpn.nixnet.com` routes to `linuxch:3978`, update each Azure Bot resource
to:

```text
https://vpn.nixnet.com/api/messages
```

This endpoint is also captured in:

```text
examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml
```

## Security Notes

The first listener is a dogfood ingress boundary, not a production-ready Bot
Framework receiver. It must be hardened before general use:

- validate Bot Framework JWT signatures and allowed audiences
- reject unsigned activities outside local tests
- store conversation references for proactive messaging
- handle install/update/delete conversation events explicitly
- add structured request metrics and trace context propagation

Until Bot Framework JWT validation exists, expose the listener only through the
intended controlled route.
