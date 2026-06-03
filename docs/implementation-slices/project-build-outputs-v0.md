# Project Build Outputs v0

Status: backlog

Date: 2026-06-03

## Product Goal

Create a first-class way to build a concrete Agentic Mesh project from a
project overlay.

The system repository should provide reusable generators and templates. Each
project folder should receive its own deployment outputs under that project
folder, such as Docker Compose, Terraform, Helm charts, or future enterprise
deployment formats.

## Boundary Decision

Project build outputs belong to the project, not the system root.

For a project at:

```text
examples/projects/agentic-mesh-dev/
```

Generated and curated deployment output should live under:

```text
examples/projects/agentic-mesh-dev/deploy/
```

The system root may keep templates, schema, tests, and generator code, but not
project-specific deployment manifests.

## Initial Output Targets

### Docker Compose

Purpose:

- local development
- small-team self-managed deployments
- dogfood runtime on one VM

Expected output:

```text
deploy/compose/docker-compose.yml
deploy/compose/docker-compose.<environment>.yml
```

### Terraform

Purpose:

- repeatable cloud resource provisioning
- enterprise review of infrastructure changes
- cloud-native message, state, secret, identity, and telemetry resources

Expected output:

```text
deploy/terraform/
```

Backlog features:

- Azure Container Apps or AKS profile
- Azure Service Bus, Table Storage, Blob Storage, Key Vault, and managed
  identity modules
- AWS ECS/EKS profile
- SQS/SNS/EventBridge, DynamoDB, S3, and Secrets Manager modules
- environment variable and secret reference mapping from project config
- generated README with apply/destroy commands and required permissions

### Helm

Purpose:

- Kubernetes-native enterprise deployment
- GitOps workflows
- platform-team customization through values files

Expected output:

```text
deploy/helm/agentic-mesh/
deploy/helm/values.<environment>.yaml
```

Backlog features:

- one Deployment per router, control-plane, connector, and role instance
- ConfigMaps for non-secret project config
- Secret references without secret values
- OTEL Collector integration
- liveness/readiness probes
- resource requests and limits
- hibernation and wake integration with Kubernetes scale behavior

## Proposed CLI Shape

Future command:

```powershell
python -m agentic_mesh.cli build-project --project-root examples/projects/agentic-mesh-dev --target compose
python -m agentic_mesh.cli build-project --project-root examples/projects/agentic-mesh-dev --target terraform --environment azure-dev
python -m agentic_mesh.cli build-project --project-root examples/projects/agentic-mesh-dev --target helm --environment aks-dev
```

The command should:

- load system defaults from `--config-root`
- load project config from `<project-root>/agentic-mesh/project.yaml`
- validate role instances, connector channels, state paths, and deployment
  target requirements
- write generated output under `<project-root>/deploy/<target>/`
- refuse to write outside the project folder
- preserve hand-edited overlays when the target supports overlays
- emit a build manifest describing generated files, source config versions, and
  target assumptions

## Acceptance Criteria

- A project folder contains `agentic-mesh/project.yaml`.
- `build-project --target compose` generates a Compose file under the project
  folder, not the system root.
- Generated Compose includes only roles configured for that project.
- Connector services are generated from project connector config.
- Runtime state paths resolve under the project folder by default.
- The generated output mounts the reusable system runtime image and system
  config separately from project config and workspace repos.
- The generator has tests proving it refuses path escapes.
- Terraform and Helm targets are recorded as backlog targets with stable output
  directories and expected responsibilities.

## Non-Goals For First Build Slice

- Full Terraform implementation.
- Full Helm implementation.
- Enterprise UI for editing deployment targets.
- Secret value generation or storage.
- Cloud resource creation from the open source CLI.
