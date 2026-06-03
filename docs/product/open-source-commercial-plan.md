# Open Source And Commercial Plan

Status: draft recommendation

Date: 2026-06-03

This document is product and licensing strategy, not legal advice. Before
publishing the repository or taking enterprise money, have counsel review the
license, contribution model, trademarks, and commercial terms.

## Executive Recommendation

Open source Agentic Mesh core under the Apache License 2.0.

Use an open-core business model, but keep the open source core genuinely useful.
Individuals, consultants, internal platform teams, and small companies should be
able to run a real project-scoped role-agent mesh themselves with Docker Compose,
local storage, file-backed queues, Git-backed artifacts, OpenTelemetry output,
starter role packs, worker adapter interfaces, and at least one basic
collaboration connector.

Commercial value should come from enterprise adoption friction:

- supported deployments
- advanced control-plane UI
- SSO and RBAC
- fleet and multi-project operations
- managed cloud queue and storage backends
- policy packs
- audit and compliance reporting
- cost dashboards
- premium collaboration features
- support, onboarding, and role-template customization

The strongest positioning is:

> Agentic Mesh is the open, self-hostable runtime for durable enterprise role
> agent networks: role-owned work, inspectable queues, human gates,
> collaboration-channel handoffs, Git-backed evidence, and observable runtime
> state.

Do not compete head-on as "another agent framework." Compete as the enterprise
runtime layer around long-running role agents.

## Current Feature Inventory

The repository already has the beginnings of a credible open source core.

Implemented or started:

- Python source-layout package with CLI entry points.
- Organization defaults, role templates, project overlays, flow templates, and
  reusable human response templates loaded from YAML.
- Project schema and response-type schema.
- Starter software-delivery role pack.
- Role instances with stable ids and support for multiple instances of the same
  role.
- File-backed role inboxes, connector outboxes, claimed/completed folders, and
  inspectable JSON payloads.
- Append-only JSONL event journal.
- Deterministic worker adapter stub and `WorkerAdapter` boundary.
- Runtime handoff through project-configured flow states.
- Human response gates and normalized human response messages.
- Local Teams-style connector that renders Adaptive Cards.
- Microsoft Graph and Bot Framework Teams connector adapters in early form.
- Teams bot ingress listener for Adaptive Card submits.
- Auth method catalog and per-role worker auth plan.
- Basic lifecycle store and queue-aware hibernate/wake tick.
- Docker Compose topology for router, control-plane, role agents,
  Teams connector, and OpenTelemetry collector.
- Tests covering config, auth, message store, runtime flow, lifecycle,
  documents, messaging, and connectors.

Designed but not yet mature:

- Real `codex-cli`, OpenAI, Anthropic, Claude Code, MiniMax, DeepSeek, and other
  worker adapters.
- Production-grade router separated from runtime handoff behavior.
- Real control-plane service process with health checks, leases, concurrency,
  hibernation grace windows, and restart integration.
- OpenTelemetry SDK instrumentation rather than journaled trace stand-ins.
- Durable cloud queue and state adapters.
- Secret provider adapters.
- Slack/email/web/CLI collaboration connectors.
- Multi-project topology management.
- Control-plane UI.
- Enterprise policy enforcement, audit exports, and cost reporting.

## Market Comparison

### LangGraph And LangSmith

LangGraph is a low-level orchestration framework for stateful, long-running
agents. Its open source library is MIT licensed and emphasizes durable
execution, human-in-the-loop, memory, debugging, and production deployment.
LangSmith is the commercial layer for observability, evaluation, prompt
engineering, and deployment. LangChain's self-hosted LangSmith docs describe
self-hosting as an Enterprise-plan add-on and list services such as frontend,
backend API, queue, ClickHouse, PostgreSQL, Redis, and optional blob storage.

Implication for Agentic Mesh:

- Do not try to out-LangGraph LangGraph on graph orchestration.
- Differentiate on role-template governance, project overlays, collaboration
  channels, Git-owned evidence, and enterprise handoff/accountability.
- Commercial self-hosted control-plane and managed backends are a market-proven
  line.

### CrewAI And CrewAI AMP

CrewAI's open source story is strong: broad multi-agent orchestration, planning,
tools, memory, collaboration, and checkpointing. CrewAI AMP is the commercial
platform for deploying, monitoring, scaling, API access, tool repository,
webhook streaming, and no-code/low-code Crew Studio.

Implication for Agentic Mesh:

- CrewAI sells operational production experience around an open framework.
- Agentic Mesh can use the same open-core shape while aiming at enterprise
  project operations rather than autonomous task crews.
- Quick commercial wins should look like deployment, monitoring, support,
  collaboration setup, and governance, not withheld agent definitions.

### OpenAI Agents SDK

The OpenAI Agents SDK is MIT licensed and provides agents, handoffs, tools,
guardrails, human-in-the-loop mechanisms, sessions, tracing, and sandbox agents.
It is a strong SDK-level building block, not a full enterprise project runtime.

Implication for Agentic Mesh:

- Treat SDKs like this as worker or orchestration adapter candidates.
- Keep Agentic Mesh product semantics provider-neutral: role ownership, queues,
  journal, control-plane lifecycle, connectors, and artifacts should not depend
  on OpenAI-specific object models.

### Microsoft AutoGen, Semantic Kernel, And Microsoft Agent Framework

AutoGen is now presented as maintenance-mode, with Microsoft recommending
Microsoft Agent Framework for new projects. Semantic Kernel's repository also
points to Microsoft Agent Framework as an enterprise-ready successor with
multi-provider orchestration and A2A/MCP interoperability. AutoGen Studio is
explicitly described as prototyping-oriented rather than production-ready.

Implication for Agentic Mesh:

- Microsoft is moving toward enterprise-grade agent SDKs, not project-scoped
  role networks.
- Agentic Mesh should integrate well with Microsoft identity, Teams, Graph,
  Azure deployment, and OTEL, but avoid becoming semantically Azure-only.
- A Microsoft-friendly deployment package is commercially attractive.

### Dapr Agents

Dapr Agents is Apache-2.0 licensed and focuses on autonomous, resilient,
observable agents with durable workflow orchestration. Dapr's positioning
emphasizes retries, crash recovery, scale-to-zero, statefulness, and telemetry.

Implication for Agentic Mesh:

- Dapr is a potential substrate or integration path, especially for durable
  workflows and scale-to-zero.
- Agentic Mesh should not build generic distributed application primitives if
  Dapr can eventually supply them behind adapters.
- The value remains role mesh semantics, not workflow plumbing.

## Open Source Boundary

Open source core should include:

- Role template, organization defaults, project override, and role-instance
  model.
- Runtime for claiming work, invoking worker adapters, journaling outcomes, and
  emitting handoffs.
- Worker adapter interface and at least one useful open adapter or local stub.
- Collaboration connector interface.
- Basic Microsoft Teams connector where platform terms and permissions allow.
- Local connector emulator for development and tests.
- File-backed message, connector outbox, state, artifact, and event-journal
  adapters.
- Docker Compose deployment profile.
- Basic control-plane lifecycle, hibernation, wake-on-inbox, and status.
- OpenTelemetry instrumentation and collector config.
- CLI for validation, enqueue, run, status, auth plan, control-plane tick, and
  connector processing.
- Configuration schemas.
- Starter role packs and starter flows.
- Documentation for self-managed local and small-team use.

Commercial features should include:

- Advanced web control-plane UI.
- Multi-project and multi-tenant topology management.
- Enterprise SSO, SCIM, RBAC, role-based approvals, and identity mapping.
- Managed Azure/AWS deployment packages, operators, and upgrade automation.
- Supported Azure Service Bus, Table Storage, Blob Storage, Key Vault, AWS SQS,
  DynamoDB, S3, and Secrets Manager adapters.
- Fleet health, incident, and wake/hibernate dashboards.
- Cost and token dashboards with budgets, alerts, and chargeback.
- Enterprise policy packs for regulated work, tool permissions, model routing,
  data residency, retention, PII redaction, and approval gates.
- Compliance and audit reporting packs, including exportable evidence bundles.
- Premium Teams/Slack setup automation, app registration assistants, and channel
  provisioning.
- Private connector packs for Jira, Azure DevOps, ServiceNow, SharePoint,
  Confluence, GitHub Enterprise, GitLab, and internal systems.
- Premium role-template customization, onboarding, training, and support.

The open source version can be operationally self-managed and more manual. It
should not be artificially unreliable, limited by agent count, blocked from
production use, or missing the basic ability to complete a project flow.

## Commercial Quick Wins

### 0-30 Days

- Publish an enterprise design-partner offer: fixed-fee discovery, installation,
  Teams setup, one role-pack customization, and two weeks of support.
- Create an "Enterprise Readiness Assessment" service that reviews a customer's
  desired roles, connectors, auth, compliance needs, and deployment target.
- Package the first paid deliverable as a written deployment and governance
  plan, not a large proprietary product.
- Add public docs that make the open core credible: `LICENSE`, `NOTICE`,
  `SECURITY.md`, `CONTRIBUTING.md`, `GOVERNANCE.md`, `CODE_OF_CONDUCT.md`,
  third-party notices, and a clear open-core feature matrix.
- Create a repeatable Teams setup guide and sell assisted Microsoft 365 setup.
- Create one polished demo path: local Docker Compose, a sample work item, human
  approval card, handoff chain, generated artifacts, and journal review.

### 30-60 Days

- Build a read-only control-plane dashboard as the first commercial UI:
  projects, roles, queues, hibernated/running state, journal events, and
  connector status.
- Add an enterprise deployment blueprint for Azure Container Apps or AKS with
  Key Vault, OpenTelemetry Collector, and managed storage placeholders.
- Add a supportable adapter certification checklist for worker providers and
  collaboration connectors.
- Offer paid custom role packs for software delivery, architecture review,
  security review, release governance, and incident response.
- Add an audit evidence bundle command in the open core, then sell richer
  compliance report templates and advisory.

### 60-90 Days

- Release a paid "Enterprise Control Plane Preview" with SSO/RBAC, multi-project
  read-only views, health alerts, and cost summaries.
- Offer managed or assisted private-cloud deployments.
- Add first premium policy pack: model/provider allowlists, approval gates by
  risk tier, write-path restrictions, and retention defaults.
- Add premium connector automation for Teams app registration/channel setup or
  Slack workspace setup.
- Create enterprise support tiers with response targets, onboarding hours, and
  role-template customization credits.

## License Recommendation

Use Apache License 2.0 for the open source core.

Why Apache-2.0:

- It is permissive, so individuals, consultancies, small companies, and
  enterprises can adopt it without copyleft friction.
- It includes an express patent license and patent retaliation terms, which
  matters for infrastructure, agent orchestration, and enterprise buyers.
- It is common in cloud-native infrastructure and compatible with the current
  dependency direction.
- It allows proprietary commercial add-ons, hosted services, support, and
  enterprise distributions without relicensing the open core.

Why not MIT:

- MIT is simpler and popular, and current Python dependencies would allow it.
- It does not provide the same explicit patent grant. For an enterprise runtime,
  Apache-2.0 is the better trust signal.

Why not AGPL:

- AGPL can protect against hosted closed forks, but it will reduce enterprise
  adoption and complicate embedding inside customer platforms.
- Agentic Mesh should monetize operations, governance, support, and premium
  enterprise capabilities rather than relying on network copyleft leverage.

Why not Elastic License, BSL, SSPL, Commons Clause, or other source-available
licenses for core:

- They are not standard open source licenses.
- They weaken the "open source core" claim and create buyer/community
  ambiguity.
- They can still be used for separate commercial components if needed, but the
  public core should remain OSI-open.

## Dependency License Notes

Current direct dependencies:

- PyYAML: MIT.
- pytest: MIT, development only.

Current container/runtime components:

- Python base image and standard library: compatible with Apache-2.0
  distribution, with normal Python/third-party notices.
- OpenTelemetry Collector image: Apache-2.0 project; include notices when
  redistributing packaged images.

Likely compatible future dependencies:

- OpenTelemetry Python: Apache-2.0.
- Microsoft Graph SDK for Python: MIT.
- OpenAI Agents SDK: MIT.
- LangGraph library: MIT, if used as an optional worker/orchestration adapter.
- Dapr Agents and Dapr runtime: Apache-2.0, if used behind deployment/runtime
  adapters.
- FastAPI: MIT.
- Pydantic: MIT.
- SQLAlchemy and Alembic: MIT.
- Azure SDK libraries: generally MIT.
- AWS SDK/Boto3: generally Apache-2.0.
- Redis client libraries: commonly MIT.
- PostgreSQL client libraries: commonly LGPL or PostgreSQL-style depending on
  package; verify exact package before adding.

Dependency policy:

- Allow permissive MIT, BSD, ISC, Apache-2.0, and PostgreSQL-style licenses in
  the open source core.
- Allow MPL-2.0 only after review, preferably isolated to optional adapters.
- Avoid GPL, LGPL, AGPL, SSPL, Commons Clause, Polyform, BSL, Elastic License,
  and custom source-available licenses in the core unless explicitly reviewed
  and isolated.
- Keep provider SDKs optional where possible so one vendor's license, terms, or
  platform policy does not become a product-semantic dependency.
- Generate and publish `THIRD_PARTY_NOTICES.md` before the first public release.
- Add a dependency review step to CI before accepting new runtime dependencies.

## Contribution And Governance Model

Before public launch:

- Add `LICENSE` with Apache-2.0.
- Add `NOTICE` for Agentic Mesh.
- Add `CONTRIBUTING.md` with Developer Certificate of Origin sign-off.
- Add `SECURITY.md`.
- Add `CODE_OF_CONDUCT.md`.
- Add `GOVERNANCE.md` describing maintainers, decision process, and commercial
  boundary.
- Add `TRADEMARKS.md` or a short trademark section: the code license does not
  grant rights to the Agentic Mesh name, logos, or commercial marks.

Start with DCO rather than a CLA. It keeps contribution friction low. Revisit a
CLA only if large corporate contributions, relicensing needs, or foundation
governance require it.

## Launch Readiness Checklist

- Decide repository owner, package namespace, trademark owner, and copyright
  holder.
- Add Apache-2.0 license and notices.
- Remove secrets, tenant ids, real tokens, customer names, and internal-only
  paths from examples and state.
- Move generated runtime `state/` out of public examples unless sanitized.
- Make the README public-first: what it is, who it is for, quickstart, current
  limitations, and roadmap.
- Add a "Commercial Offering" page that is transparent about the open source
  boundary.
- Add a "Competitors And Integrations" page that positions Agentic Mesh as a
  runtime around agent SDKs rather than a replacement for every SDK.
- Add issue templates for bug, feature, connector request, and role-pack
  request.
- Add a release process, semantic versioning policy, and security disclosure
  route.
- Add CI for tests, formatting, dependency/license review, and basic Docker
  build.

## Recommended Next Implementation Slices

### Open Source Hardening Slice

Acceptance criteria:

- Repository has Apache-2.0 `LICENSE`, `NOTICE`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and `GOVERNANCE.md`.
- README includes a self-managed quickstart that works from a clean checkout.
- Generated state and examples are sanitized for public release.
- CI runs tests and a dependency/license check.
- `THIRD_PARTY_NOTICES.md` can be generated repeatably.

### Commercial Readiness Slice

Acceptance criteria:

- A feature matrix explains open source, paid self-managed, and managed/assisted
  enterprise offerings.
- A design-partner package is documented with scope, deliverables, and
  exclusions.
- A deployment assessment template exists for customer discovery.
- A read-only control-plane dashboard spike is scoped with mock data and real
  data sources.

### Enterprise Control-Plane Preview Slice

Acceptance criteria:

- Web UI shows projects, role instances, lifecycle state, queue depth, connector
  outbox status, and recent journal events.
- Authentication boundary is explicit, even if local preview auth is minimal.
- UI reads from existing core state and journal ports rather than creating a
  hidden configuration database.
- The OSS CLI remains fully usable without the commercial UI.

## Sources Checked

- LangGraph repository: https://github.com/langchain-ai/langgraph
- LangChain self-hosted LangSmith docs:
  https://docs.langchain.com/langsmith/self-hosted
- CrewAI open source page: https://crewai.com/open-source
- CrewAI AMP docs: https://docs.crewai.com/en/enterprise/introduction
- OpenAI Agents SDK docs:
  https://developers.openai.com/api/docs/guides/agents
- OpenAI Agents SDK Python repository:
  https://github.com/openai/openai-agents-python
- Microsoft AutoGen repository: https://github.com/microsoft/autogen
- Microsoft Semantic Kernel repository:
  https://github.com/microsoft/semantic-kernel
- Dapr Agents repository: https://github.com/dapr/dapr-agents
- Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0.html
- OSI license list: https://opensource.org/licenses
- PyYAML project page: https://yaml.com/projects/pyyaml/
- pytest license page: https://docs.pytest.org/en/stable/license.html
- OpenTelemetry Python repository:
  https://github.com/open-telemetry/opentelemetry-python
- Microsoft Graph SDK Python repository:
  https://github.com/microsoftgraph/msgraph-sdk-python
