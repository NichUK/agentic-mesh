# Repository Topology

Status: draft design

Date: 2026-06-02

## Intent

Agentic Mesh needs a clean separation between the product/runtime repository
and the repositories or workspaces that describe actual projects.

The Agentic Mesh root repository contains the system, default configuration,
starter templates, examples, and runtime code. Real project repositories contain
project overlays, work items, documentation, evidence, and project-specific
runtime state references.

## Repository Types

### System Repository

The system repository is this product repository.

It owns:

- runtime source code
- storage, worker, connector, lifecycle, and telemetry ports
- Docker image definitions and reusable deployment templates
- default role templates
- default organization policy templates
- default connector and storage templates
- stock flow templates
- configuration schemas for project overlays and pack validation
- example projects with no credentials
- architecture decisions and product documentation
- test fixtures and smoke-test examples

It must not own:

- customer project secrets
- live connector credentials
- personal Codex auth files
- customer-specific Teams, Slack, GitHub, or cloud tokens
- production runtime queues or state
- confidential project artifacts unless deliberately included as examples

### Project Repository Or Workspace

A project repository or workspace owns a concrete project overlay.

It may contain:

- `agentic-mesh/project.yaml`
- `deploy/compose/docker-compose.yml`
- generated or curated deployment outputs under `deploy/`
- project SDLC flow template references or overlays
- project role overrides and instance counts
- project documentation and evidence
- project-specific write boundaries
- connector channel mappings by logical name
- references to secret names or secret provider paths
- committed audit snapshots where useful

It must not contain secret values. It should reference secrets by logical name,
for example `codex-agent-token`, `teams-connector-client-secret`, or a cloud
secret provider path.

### Runtime State

Runtime state is separate from both source code and project configuration.

Local state can live under a generated `state/` directory and stay ignored by
Git. Cloud deployments should store queues, leases, connector cursors, and
live lifecycle state in configured backends behind Agentic Mesh ports.

The event journal remains append-only and inspectable, but teams can choose
which snapshots are committed back to a project repo.

## Example Projects

Example projects live under `examples/projects/` as project folders, not as
loose YAML files.

The current dogfood example is:

```text
examples/projects/agentic-mesh-dev/
  agentic-mesh/project.yaml
  deploy/compose/docker-compose.yml
  deploy/compose/docker-compose.linuxch.yml
```

It is safe to keep in the system repository because it contains no credentials
and demonstrates project overlay and deployment-output structure.

## Future Installation Shape

A local developer installation should eventually look like this:

```text
agentic-mesh/                 # system repo
  src/
  config/
    roles/
    flows/
    schemas/
  examples/

agentic-mesh-projects/
  quantauma/
    agentic-mesh/project.yaml
    deploy/compose/docker-compose.yml
    deploy/terraform/
    deploy/helm/
    state/                    # ignored local runtime state
    docs/
    evidence/
    releases/
  another-project/
    agentic-mesh/project.yaml
```

The control-plane receives a system repo path plus one or more project repo
paths. It loads defaults from the system repo, overlays project configuration
from each project, and mounts each role instance against the correct project
workspace and state location.

## Project Build Boundary

A project is the deployment boundary. It declares which role-agent instances
exist, which collaboration connector/team/channel mapping is used, which
workspace repositories are mounted, and which runtime state path belongs to the
project.

Project build outputs belong under the project folder:

```text
<project-root>/
  agentic-mesh/project.yaml
  deploy/
    compose/
    terraform/
    helm/
  state/
```

The system repository may provide generator code, schema, and templates, but it
must not accumulate concrete project deployment files at its root. The current
dogfood Compose files are intentionally under
`examples/projects/agentic-mesh-dev/deploy/compose/`.

## Container Image Boundary

Agentic Mesh containers should not bake organization or project configuration
into the image. The image is a runtime artifact containing application code and
runtime dependencies. Configuration, project workspaces, runtime state, and
secrets are runtime inputs.

Current dogfood container paths:

```text
/mesh/system                    # mounted system repo, preferably read-only
/mesh/project                   # mounted project folder with agentic-mesh/project.yaml
/mesh/workspaces/agentic-mesh   # mounted project repository/workspace
/mesh/project/state             # runtime queues, journals, lifecycle, secrets
```

The project file declares the workspace and repository roots that agents use
for real work:

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

`workspace.root` is resolved under `AGENTIC_MESH_WORKSPACE_ROOT` unless it is
an absolute mounted path. Repository `path` values are then resolved beneath
that workspace. Role `write_paths` and flow `artifact_path` values are relative
to the effective project workspace, so agents can edit source, tests, docs,
evidence, and release records inside the mounted project repository.

The CLI accepts explicit paths for these boundaries:

```text
--config-root
--project-file
--workspace-root
--state-root
```

The same values can be supplied with environment variables:

```text
AGENTIC_MESH_CONFIG_ROOT
AGENTIC_MESH_PROJECT_FILE
AGENTIC_MESH_WORKSPACE_ROOT
AGENTIC_MESH_STATE_ROOT
```

This supports a single centrally published runtime image per container type,
with organization and project configuration mounted from external volumes,
repositories, or managed configuration stores.

## Open Questions

- Should project overlays use `agentic-mesh/project.yaml` or
  `.agentic-mesh/project.yaml`?
- Should stock role packs and stock flow packs live under `config/` or
  `packs/`?
- What project build targets should be generated first after Compose:
  Terraform, Helm, cloud-specific Compose overlays, or enterprise GitOps
  bundles?
