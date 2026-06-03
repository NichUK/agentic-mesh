# Product Manager Worklist

Status: sponsor-editable adoption output

## Role View

Agentic Mesh should be positioned as the open, self-hostable runtime for
durable enterprise role-agent networks. The product must make a sharp promise:
role-owned work, project-scoped configuration, visible collaboration,
Git-backed evidence, human gates, observability, and enterprise operations.

## Competitive Positioning

- Against LangGraph: Agentic Mesh is not the graph SDK; it is the governed
  project/runtime layer around long-running roles and enterprise channels.
- Against CrewAI: Agentic Mesh is less "crew automation" and more durable role
  organization, evidence, handoff, and lifecycle management.
- Against OpenAI Agents SDK: Agentic Mesh can use provider SDKs behind worker
  adapters while preserving provider-neutral product semantics.
- Against Microsoft Agent Framework: Agentic Mesh should be Microsoft-friendly
  for Teams/Entra/Azure but not Microsoft-bound.
- Against Dapr Agents: Agentic Mesh may eventually run on Dapr primitives, but
  owns role mesh, artifacts, and enterprise collaboration semantics.

## Product Feature List

- MVP OSS: local project mesh, role templates, project overrides, file queues,
  event journal, Teams ingress/egress, basic lifecycle, starter SDLC role pack,
  Docker Compose, CLI, and docs.
- MVP commercial: enterprise assessment, assisted deployment, Teams/Entra
  setup, read-only control plane, support, and role-pack customization.
- Enterprise expansion: SSO/RBAC, policy packs, audit reports, managed queue
  and state adapters, cost dashboards, premium connectors, multi-project fleet
  operations, Terraform/Helm/Kubernetes packages.

## Outstanding Work

- Write a public README that leads with the product promise and shows one real
  self-managed demo path.
- Add open-core feature matrix and commercial page.
- Define design-partner offer: scope, deliverables, support window, exclusions,
  and success criteria.
- Define first three role packs: software delivery, architecture review, and
  release governance.
- Create product acceptance criteria for "agent adoption of a project".
- Define telemetry/cost views needed for an enterprise buyer.
- Define connector roadmap with Teams first, Slack second, and GitHub/Jira or
  Azure DevOps as work-management integrations.
- Define customer-facing language for "router does not decide; agents decide".

## Risks And Decisions

- Risk: Product appears unavailable when worker credentials are missing.
  Mitigation: make credential setup and blocked-state recovery explicit in the
  onboarding path.
- Risk: Commercial line looks arbitrary. Mitigation: keep OSS useful and charge
  for enterprise adoption, governance, support, and operations.
- Decision needed: name and packaging for first commercial preview.

## Suggested Acceptance Criteria

- A buyer can understand "why Agentic Mesh" in under two minutes.
- A small team can run a useful OSS mesh.
- An enterprise can see a credible path to supported deployment and governance.
