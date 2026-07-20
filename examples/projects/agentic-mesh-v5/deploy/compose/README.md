# LinuxCH V5 Compose deployment

This is the canonical project-owned Linux Docker deployment for Agentic Mesh
V5. It contains Postgres, the one-shot migration job, control API,
OpenTelemetry collector, and all 16 pre-provisioned role instances.

## External inputs

Create these under the project state boundary before deployment:

- a populated Compose environment file based on `.env.example`;
- `runtime.env`, including the Postgres connection URL and OTLP endpoint;
- a random Postgres password file;
- the API principals file and one API token file per role;
- a per-instance Codex volume seeded from the current authenticated Codex
  login;
- a project-scoped GitHub CLI `hosts.yml` under the configured GitHub directory;
- a current delegated Graph access token at
  `credentials/documents/graph/oauth-cache/projects/agentic-mesh-v5/graph`
  beneath the configured document-credential root;
- an external checkout of `agentic-mesh-config` with its mutable `state/`
  directory mounted separately (create the empty mountpoint in the checkout
  before the read-only parent bind is rendered); and
- the ordinary Agentic Mesh project source checkout plus the isolated worktree
  root.

All secret and state files must be owner-readable only and owned by the UID that
reads them in the container. In the runtime image, `mesh` is UID 10001. Do not
put secret values in Git, images, Compose YAML, logs, prompts, memory, or work
records.

The control plane derives the token path from the active manifest's exact
provider and external credential reference. It reads the file for every Graph
operation, so the Linux host can replace it atomically without restarting the
API. The document credential root is mounted only into `control`; role workers
never receive Graph credentials.

Install the supplied systemd service and timer on LinuxCH after copying
`systemd/agentic-mesh-v5-graph-token-refresh.env.example` to the external state
path named by the service. Run the service once before starting Compose, then
enable the timer:

```bash
sudo cp systemd/agentic-mesh-v5-graph-token-refresh.service /etc/systemd/system/
sudo cp systemd/agentic-mesh-v5-graph-token-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start agentic-mesh-v5-graph-token-refresh.service
sudo systemctl enable --now agentic-mesh-v5-graph-token-refresh.timer
```

The timer refreshes every 30 minutes from the existing delegated Azure CLI
login. It writes no token to stdout, uses an atomic same-directory rename, and
installs the file for UID/GID 10001. If interactive Azure reauthentication is
ever required, the service fails without replacing the last valid token.

The 16 Codex volumes are external because they must exist and contain a valid
OAuth cache before any role starts. Seed each declared volume from the current
authenticated Codex home and keep each volume private to its role instance.

## Validate and start

From this directory on LinuxCH:

```bash
docker compose --env-file /path/to/project/state/compose.env config --quiet
docker compose --env-file /path/to/project/state/compose.env --profile fleet create
docker compose --env-file /path/to/project/state/compose.env up -d
docker compose --env-file /path/to/project/state/compose.env ps
```

The migration job must finish successfully before control starts. The control
health endpoint is bound only to loopback at `http://127.0.0.1:18080` by
default. External ingress and SaaS connectors are separate integrations.

The `fleet` profile creates, but does not start, the 15 scale-to-zero specialist
containers. The normal `up -d` command starts the core and Project Manager only.
The fleet supervisor starts and stops the exact pre-provisioned containers in
`fleet.json` after it has recorded the matching durable state transition. Do
not start all fleet-profile services as an ordinary deployment step.
