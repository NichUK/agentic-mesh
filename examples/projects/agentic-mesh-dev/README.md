# Agentic Mesh Development Project

This folder is the checked-in example configuration for dogfooding Agentic Mesh
with the V4 remote-control runtime.

In live operation the project repository, deployed runtime, runtime state, and
document library should be separate mounted locations. This example remains in
the system repository only as a reusable project configuration/template.

## Identity Model

V4 separates project identity from agent-network identity:

- `project_id` names the project context, document library, work items, target
  repositories, and project-scoped institutional memory.
- `agent_network_id` names the reusable agent mesh/team. The same configured
  role agents can work across multiple projects unless a project explicitly
  configures its own dedicated agent network.

The dogfood project currently uses:

```yaml
project_id: agentic-mesh-dev
agent_network_id: agentic-mesh-dev
```

Another project can use a different `project_id` with the same
`agent_network_id` to share the agents while keeping documents, work items, and
project memory separate.

## Layout

```text
examples/projects/agentic-mesh-dev/
  agentic-mesh/
    project-v4.yaml           # V4 project overlay and role network binding
  deploy/
    compose/
      docker-compose.yml      # local V4 Compose output
      docker-compose.linuxch.yml
  state/                      # ignored local runtime state and secrets
```

## Container Boundaries

The live dogfood deployment should mount:

```text
/mesh/system                  # installed system source/runtime code
/mesh/project                 # project configuration and runtime state
/mesh/workspaces/agentic-mesh # target source repository for Agentic Mesh work
/documents                    # canonical document library, OneDrive-backed in live use
```

Generated work-item documents must land under:

```text
/documents/work-items/{work_item_id}
```

The document library is the canonical project memory. Runtime database rows make
work inspectable and recoverable, but they do not replace durable project
documentation.

## Compose

Run from the repository root:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml up -d
```

Use the Linux VM overlay for the `linuxch` dogfood deployment:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml up -d
```

Build runtime images only when system runtime code or runtime dependencies
change:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml --profile build-image build
```

Configuration, state, project files, document library, and secrets are mounted
at runtime. They must not be baked into an image.
