# Agentic Mesh Development Project

This folder is the dogfood project for building Agentic Mesh with Agentic Mesh.

It is intentionally a project workspace nested under the system repository.
The system repository still owns reusable runtime code, stock role templates,
flow templates, schemas, and product documentation. This project folder owns the
project-specific network definition, connector/team bindings, deployment
outputs, runtime state location, and future generated infrastructure artifacts.

## Layout

```text
examples/projects/agentic-mesh-dev/
  agentic-mesh/
    project.yaml              # project overlay and role network
  deploy/
    compose/
      docker-compose.yml      # local dogfood Compose output
      docker-compose.linuxch.yml
  state/                      # ignored local runtime queues, journal, secrets
```

## Compose

Run from the repository root:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml up -d
```

Use the Linux VM overlay for the `linuxch` dogfood deployment:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml up -d
```

Build the shared local runtime image only when system runtime code or runtime
dependencies change:

```powershell
docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml --profile build-image build runtime-image
```

Configuration, state, project files, and secrets are mounted at runtime. They
must not be baked into the image.

## Container Boundaries

The dogfood Compose output mounts:

```text
/mesh/system                    # system repository, read-only
/mesh/project                   # this project folder
/mesh/workspaces/agentic-mesh   # system repo as the dogfood work workspace
```

The project file is:

```text
/mesh/project/agentic-mesh/project.yaml
```

Runtime state is:

```text
/mesh/project/state
```

This keeps project deployment artifacts and runtime state inside the project
folder while still allowing dogfood role agents to work on the system repo.
