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
- an external checkout of `agentic-mesh-config` with its mutable `state/`
  directory mounted separately (create the empty mountpoint in the checkout
  before the read-only parent bind is rendered); and
- the ordinary Agentic Mesh project source checkout plus the isolated worktree
  root.

All secret and state files must be owner-readable only and owned by the UID that
reads them in the container. In the runtime image, `mesh` is UID 10001. Do not
put secret values in Git, images, Compose YAML, logs, prompts, memory, or work
records.

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
