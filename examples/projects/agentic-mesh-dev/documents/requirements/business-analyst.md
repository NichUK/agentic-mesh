# Business Analyst Worklist

Status: sponsor-editable adoption output

## Role View

Agentic Mesh has a clear enterprise problem statement: durable role-agent work
inside a project boundary, visible in collaboration tools, backed by Git and
observable runtime state. The business-analysis gap is that this is still more
architecture than packaged buyer value. We need crisp user journeys, market
boundaries, acceptance criteria, and evidence that the open source core remains
useful without weakening commercial upside.

## Alternate Offering Analysis

- LangGraph/LangSmith: strong agent graph and observability stack. Agentic Mesh
  should not compete at SDK graph level; it should compete on project-scoped
  role ownership, enterprise collaboration, Git-backed evidence, and lifecycle
  accountability.
- CrewAI/AMP: strong multi-agent team abstraction and commercial operations
  platform. Agentic Mesh should differentiate on long-running role instances,
  governed project overlays, durable queues, and enterprise handoff discipline.
- OpenAI Agents SDK: strong provider SDK and tracing. Treat it as a worker
  adapter candidate, not the product boundary.
- Microsoft Agent Framework/Semantic Kernel: strong Microsoft ecosystem fit.
  Agentic Mesh should integrate with Teams, Graph, Entra, OTEL, and Azure
  without becoming Azure-only.
- Dapr Agents: strong durable runtime substrate. Consider Dapr behind adapters
  later, while keeping Agentic Mesh semantics at the role/project layer.

## Feature List

- Open source core: project YAML, role templates, file-backed queues, event
  journal, Docker Compose, basic lifecycle, Teams connector, worker adapter
  interface, starter role pack, and CLI.
- Commercial self-managed: supported Azure/AWS deployment profiles, control
  plane UI, SSO/RBAC, managed queue/state adapters, premium connector setup,
  policy packs, audit bundles, and cost dashboards.
- Assisted enterprise services: readiness assessment, Teams/Entra setup,
  role-pack customization, deployment support, training, and governance review.

## Outstanding Work

- Define primary buyer personas: platform engineering lead, enterprise
  architecture lead, AI transformation owner, and software delivery sponsor.
- Write top five use cases with measurable outcomes and "why now" rationale.
- Turn open-core boundary into a feature matrix with OSS, paid self-managed,
  and assisted/managed columns.
- Define onboarding journey for a small team from clean checkout to first
  routed Teams directive and generated work evidence.
- Define onboarding journey for an enterprise from assessment to secure pilot.
- Add acceptance criteria for "project adoption" so a sponsor can tell when an
  agent network has actually analysed a project rather than only acknowledged.
- Create a commercial discovery questionnaire for connectors, auth, data
  residency, audit, deployment target, and role customization.

## Risks And Decisions

- Risk: Product is perceived as another agent framework. Mitigation: lead with
  enterprise role networks, project boundaries, Teams, Git evidence, and
  operations.
- Risk: OSS core looks like crippleware. Mitigation: keep local Compose and
  self-managed use genuinely capable.
- Decision needed: first paid package name, scope, price posture, and support
  promise for design partners.

## Suggested Acceptance Criteria

- Sponsor can read a one-page feature matrix and understand what is free,
  paid, and service-led.
- A new user can complete a documented local adoption run without custom help.
- A commercial prospect can receive a standard assessment output in under one
  week.
