# Enterprise Architect Worklist

Status: sponsor-editable adoption output

## Role View

Agentic Mesh is correctly framed as an enterprise runtime rather than a single
team tool. The enterprise architecture work is to keep the platform open,
portable, governable, and auditable across identity, data, deployment,
integration, and operational domains.

## Outstanding Work

- Define reference architectures for local, small-team self-managed, Azure
  enterprise, AWS enterprise, and Kubernetes enterprise profiles.
- Document system repository versus project repository boundaries, including
  where config, state, deployment outputs, evidence, and secrets belong.
- Define adapter contracts for queues, state, artifact storage, secrets,
  identity, collaboration, worker providers, and telemetry exporters.
- Create enterprise capability map: identity, access, audit, policy,
  deployment, observability, cost, data protection, and support.
- Define multi-project and multi-tenant boundaries before building a shared
  control plane.
- Add data classification and retention model for messages, journals, docs,
  prompts, tool outputs, and connector payloads.
- Define integration posture for Teams, Slack, GitHub, Azure DevOps, Jira,
  ServiceNow, SharePoint, Confluence, and SIEM platforms.
- Document cloud portability principles so Azure is first-class but not
  product-semantic.

## Risks And Decisions

- Risk: Teams/Azure dogfood creates hidden Azure-only assumptions. Mitigation:
  require each integration to enter through provider-neutral ports.
- Risk: Git as artifact authority conflicts with runtime state needs.
  Mitigation: keep runtime queues/state separate and snapshot/audit to Git.
- Decision needed: whether commercial enterprise deployment is packaged first
  for Azure Container Apps, AKS, or generic Kubernetes.

## Suggested Acceptance Criteria

- Each deployment profile has a diagram, trust boundary, secret model, and
  operational responsibilities.
- Each adapter boundary has an interface owner, open-core expectation, and
  commercial extension expectation.
- A customer architecture review can map Agentic Mesh into existing enterprise
  identity, network, observability, and governance standards.
