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
  directory mounted separately; and
- the ordinary Agentic Mesh project source checkout plus the isolated worktree
  root.

All secret and state files must be owner-readable only. Do not put their values
in Git, images, Compose YAML, logs, prompts, memory, or work records.

## Validate and start

From this directory on LinuxCH:

```bash
docker compose --env-file /path/to/project/state/compose.env config --quiet
docker compose --env-file /path/to/project/state/compose.env up -d
docker compose --env-file /path/to/project/state/compose.env ps
```

The migration job must finish successfully before control starts. The control
health endpoint is bound only to loopback at `http://127.0.0.1:18080` by
default. External ingress and SaaS connectors are separate integrations.

The fleet supervisor may stop idle specialist containers and start the exact
pre-provisioned containers listed in `fleet.json`. The Project Manager is
labelled to remain warm.
