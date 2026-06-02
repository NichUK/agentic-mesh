# Project Memory

This file records the context needed to resume Agentic Mesh in a fresh chat
after opening `C:\Dev\agentic-mesh` as the workspace.

## Origin

This project grew out of work on `C:\Dev\dev-team-ai`, an OpenAgents-based AI
dev-team prototype.

The prototype demonstrated useful ideas:

- live role agents
- role-owned documentation
- channel handoffs
- approvals
- sponsor DMs
- role workflow readiness
- project/work item state
- testing role agents through live smoke tests

But OpenAgents became a poor architectural fit. We repeatedly had to patch
basic primitives such as identity, direct messages, Studio state, action
buttons, notifier behavior, and project boundaries. The conclusion was to stop
treating OpenAgents as the foundation and design a new runtime.

## Naming

The project is now **Agentic Mesh**.

Names considered:

- `DevTeam Mesh`: rejected because the product should expand beyond software
  delivery teams.
- `Enterprise Agent Network`: accurate but too generic.
- `AgentMesh`: concise but less distinctive.
- `TeamFabric`: less explicit about role-agent execution.

## Current Repository State

The repo was created at:

```text
C:\Dev\agentic-mesh
```

It currently contains architecture and configuration starter files only. No
runtime implementation exists yet.

Commits so far:

- `8b0924a Initial Agentic Mesh architecture`
- `1f11f72 Document hibernation and open-core direction`
- restart context commit: adds `AGENTS.md` and `MEMORY.md`

## Current Design Summary

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

Key decisions:

- One container per role-agent instance.
- Role templates are permanent or semi-permanent and rarely changed.
- Project overrides specialize role templates for each project.
- A project may run multiple instances of the same role.
- Each instance has its own storage volume, inbox, outbox, journal, identity,
  tool boundary, and telemetry identity.
- The router routes messages and handoffs but does not coordinate work like an
  executive controller.
- The control-plane starts, stops, hibernates, wakes, health-checks, and
  reports status for role instances.
- Idle agents can hibernate after a grace period.
- Hibernated agents wake automatically when new work, DMs, mentions, scheduled
  work, or manual operator action requires them.
- Runtime queues are append-only and inspectable by default.
- Cloud-native backends such as Azure Service Bus, Azure Table Storage, Blob
  Storage, DynamoDB, SQS/SNS, and S3 are backend options behind ports.
- An append-only event journal is mandatory for audit and replay regardless of
  queue backend.
- Microsoft Teams is the first collaboration connector.
- Slack should be supported later through the same connector abstraction.
- Worker/model providers are adapters: Codex, OpenAI, Anthropic, Claude Code,
  DeepSeek, MiniMax, and future providers should be replaceable.
- OpenTelemetry is required from the first implementation slice.
- Git owns config, docs, decisions, stories, test evidence, release records,
  and committed audit snapshots.

## BMAD And Roles

The role model is inspired by BMAD-style SDLC roles, but Agentic Mesh uses a
distributed architecture rather than a centralized workflow controller.

Role provenance:

- The original `dev-team-ai` blueprint defined Product Manager, Solution
  Architect, optional Infrastructure Architect, Delivery Manager, QA Engineer,
  Release Manager, and AO Bridge.
- During prototyping, Enterprise Architect was added.
- Engineering was added so implementation could be handled by a role agent.
- Infrastructure Architect currently folds into Enterprise/Solution
  Architecture unless later separated.

Current role examples in this repo:

- `config/roles/product-manager.yaml`
- `config/roles/engineering.yaml`

These are starter examples, not final canonical templates. A future role
definition pass should create a proper starter role pack.

Potential starter pack:

- product-manager
- solution-architect
- enterprise-architect
- delivery-manager
- engineering
- qa-engineer
- release-manager

Potential optional packs:

- security
- cloud/infrastructure
- data
- support
- legal/compliance
- finance/procurement

## Open Source And Commercial Direction

The project should be open source with a commercial offering layered on top.

Open source should include the useful core:

- runtime
- role templates and project overrides
- local storage adapters
- event journal
- Docker Compose profile
- basic lifecycle control and hibernation
- OTEL
- worker adapter interface
- collaboration connector interface
- basic Teams connector where feasible

Commercial offering ideas:

- support
- advanced control-plane
- SSO/RBAC
- managed or assisted Azure/AWS deployment
- enterprise policy packs
- compliance and audit reporting
- cost dashboards
- managed storage/message backends
- onboarding and role-template customization

Principle: do not make the open source core hollow. Commercial value should be
enterprise operations, governance, support, and convenience.

## Recommended Next Slice

The next work should probably be an implementation-planning slice, not a large
build.

Suggested next slice:

**Local Runtime Skeleton v0**

Scope:

- Python or TypeScript project skeleton decision.
- Docker Compose topology with:
  - router
  - control-plane
  - product-manager agent
  - engineering agent instance 1
  - engineering agent instance 2
  - otel-collector
- config loader for role templates and project overrides.
- file-backed message store.
- append-only event journal.
- basic claim semantics for multi-instance engineering.
- simple worker adapter stub before real Codex integration.
- lifecycle state model with idle -> hibernating -> hibernated -> starting.

Acceptance criteria to define before coding:

- project config loads and validates
- role instances derive from template plus project override
- two engineering instances compete for one role queue without duplicate claim
- idle agent can hibernate after grace period
- hibernated agent wakes when an inbox message appears
- event journal records route, claim, hibernate, wake, and complete events
- OTEL spans or a local stand-in trace correlation exists

## Important Non-Goals For Now

- Do not rebuild the full OpenAgents prototype.
- Do not start with a heavy database.
- Do not start with Azure Service Bus as the only queue backend.
- Do not build the configuration UI before config files are stable.
- Do not make Teams the only possible UI.
- Do not hard-wire Codex or any one model provider into the runtime semantics.
- Do not make the control-plane an executive workflow decision-maker.
