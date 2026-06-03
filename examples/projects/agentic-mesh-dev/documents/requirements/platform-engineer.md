# Platform Engineer Worklist

Status: sponsor-editable adoption output

## Role View

The platform shape is promising: one container per role instance, project
folders as deployment boundaries, Compose first, and future Terraform/Helm.
The gap is a reproducible build/deploy workflow with health, reload, secrets,
and observability that platform teams can operate without manual repair.

## Outstanding Work

- Implement `build-project --target compose` to generate project-scoped
  Compose files under `<project>/deploy/compose/`.
- Add backlog-ready Terraform and Helm target structures under
  `<project>/deploy/terraform/` and `<project>/deploy/helm/`.
- Add config reload signal for listener/ingress services and document the
  operator command/API.
- Add health checks for router, control-plane, role agents, Teams connector,
  Graph ingress, and OTEL collector.
- Add deployment manifest recording source commit, project config hash,
  generated files, environment, and image tags.
- Add secret reference validation without printing secret values.
- Add container naming, network naming, and volume naming conventions derived
  from project id and environment.
- Add operational commands: status, queue depth, connector outbox depth,
  cursor status, hibernation status, and last error.
- Add log/trace correlation across connector receive, route, role claim,
  worker run, doc update, and acknowledgement.

## Risks And Decisions

- Risk: Manual VM operations hide drift. Mitigation: deploy from commit and
  record runtime version.
- Risk: Compose and Kubernetes output diverge. Mitigation: use a common
  project deployment model and target-specific renderers.
- Decision needed: first enterprise target for supported paid deployment.

## Suggested Acceptance Criteria

- A clean project folder can generate and run Compose without root-level
  project files.
- A config change can be reloaded by signal and verified without recreating
  the listener container.
- Platform status shows connector cursor, queue depths, and failed sends.
