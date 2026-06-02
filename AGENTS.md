# Agent Instructions

This repository is the new home for **Agentic Mesh**.

Use this file as standing guidance when a coding agent starts in this folder.

## Project Identity

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

The project is not named `devteam`, because the architecture should expand
past software delivery teams. The first role pack may be BMAD-inspired
software delivery, but the platform should support broader enterprise role
networks later.

## Current Architecture Direction

Read these first:

- `README.md`
- `docs/architecture.md`
- `docs/architecture/agentic-mesh-design.md`
- `docs/architecture/decisions.md`

Committed architecture decisions:

- `ADR-001`: Agentic Mesh enterprise runtime direction.
- `ADR-002`: Queue-aware agent hibernation and open-core direction.

Core principles:

- One container per role-agent instance.
- Permanent role templates are stable and rarely updated.
- Projects apply project-specific overrides to role templates.
- A project can run multiple instances of the same role.
- Agents own their inbox/outbox/journal and role-specific storage.
- A router routes messages and handoffs but does not make workflow decisions.
- A control-plane supervises lifecycle, health, hibernation, and wake-up but
  does not make product/architecture/implementation/QA/release decisions.
- Storage, collaboration connectors, worker/model providers, and deployment
  profiles are adapter boundaries.
- Git owns configuration, documentation, decisions, stories, evidence, release
  records, and committed audit snapshots.
- Runtime queues are inspectable and append-only by default, with optional
  cloud-native backends.
- Every deployment should emit OpenTelemetry logs, traces, and metrics.

## Role Model

Separate these concepts:

- role template: stable organization/product definition, for example
  `engineering`
- organization defaults: provider, policy, connector, storage, and telemetry
  defaults
- project override: project-specific instructions, write paths, channels,
  tools, model choice, and instance count
- role instance: concrete running worker, for example
  `quantauma.engineering.1`

Current example files:

- `config/roles/product-manager.yaml`
- `config/roles/engineering.yaml`
- `config/projects/example-project.yaml`

These are starter examples, not final canonical role templates.

## Product Positioning

The project should be open source with a commercial offering on top.

Open source core should remain useful on its own:

- role template and project override model
- role-agent runtime
- worker adapter interface
- local file-backed storage adapters
- append-only event journal
- Docker Compose deployment profile
- basic control-plane for local lifecycle and hibernation
- OpenTelemetry instrumentation
- collaboration connector interface
- basic Microsoft Teams connector where feasible
- starter role packs

Commercial offerings may include:

- advanced control-plane UI
- SSO/RBAC
- supported cloud deployments
- managed storage/message backends
- policy packs
- compliance and audit reporting
- cost dashboards
- premium support
- onboarding and role-template customization

Do not design the open source core as crippleware. Commercial features should
improve enterprise adoption, governance, operations, and support.

## Implementation Bias

When implementing:

- Prefer small, testable slices.
- Start local-first with Docker Compose and file-backed adapters.
- Keep cloud-native services behind ports/interfaces.
- Keep queue delivery separate from the event journal.
- Avoid hard-wiring Azure, AWS, Teams, Codex, or any specific LLM into product
  semantics.
- Make runtime state inspectable.
- Preserve role-instance identity across hibernation and wake-up.
- Use clear config files before building a configuration UI.

## Documentation Expectations

Update docs as design decisions are made.

Use:

- `docs/architecture/decisions.md` for ADRs.
- `docs/architecture/agentic-mesh-design.md` for the main design.
- `docs/architecture.md` for the summary view.
- `README.md` for project positioning and quick orientation.
- `MEMORY.md` for project history and current state for future chats.

If a new implementation slice is defined, document its acceptance criteria
before coding.

## Git

This repo was initialized locally at `C:\Dev\agentic-mesh`.

Current branch is `main`.

Keep commits focused. Do not mix unrelated changes from the old
`C:\Dev\dev-team-ai` prototype repo into this repository.

