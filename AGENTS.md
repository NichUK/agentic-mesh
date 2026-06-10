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
- `ADR-003`: Project-scoped build and deployment outputs.

Core principles:

- One container per role-agent instance.
- Permanent role templates are stable and rarely updated.
- Projects apply project-specific overrides to role templates.
- A project can run multiple instances of the same role.
- Agents own their inbox/outbox/journal and role-specific storage.
- The document library is canonical project memory. Role memory is a concise,
  source-linked accelerator and must cite documents, work items, or events.
- A router routes messages and handoffs but does not make workflow decisions.
- A control-plane supervises lifecycle, health, hibernation, and wake-up but
  does not make product/architecture/implementation/QA/release decisions.
- Storage, collaboration connectors, worker/model providers, and deployment
  profiles are adapter boundaries.
- Git owns configuration, documentation, decisions, stories, evidence, release
  records, and committed audit snapshots.
- Project folders own concrete deployment outputs such as Compose, Terraform,
  Helm, connector/team bindings, and local runtime state boundaries.
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
- `examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml`
- `examples/projects/example-project/agentic-mesh/project.yaml`

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
- Make project documentation visible as it is produced so sponsors can inspect
  or add direction before work reaches the next lifecycle gate.
- Preserve role-instance identity across hibernation and wake-up.
- Use clear config files before building a configuration UI.
- Keep system-repo artifacts separate from project artifacts. The system repo
  owns reusable runtime code, templates, schemas, and generators. Project
  folders own `agentic-mesh/project.yaml`, `deploy/`, connector/team bindings,
  and ignored local `state/`.

## Collaboration Behaviour

When a human or another agent gives an instruction:

- Acknowledge receipt promptly.
- Confirm the interpreted intent in plain language.
- If the instruction starts long-running work, send an initial confirmation
  before or as the work begins so the sender knows the mesh heard them.
- Keep the sender updated during long-running work, especially across handoffs,
  blockers, approval waits, or deployment steps.
- If the instruction cannot be started because routing, permissions, connector
  wiring, or required context is missing, say so explicitly and record the
  blocker instead of silently accepting the message.
- For plan and document review loops, write visible `## Review Log` comments
  with concrete requested changes and dispositions.
- Resolve role disagreements through written review loops first. Request
  mediation only after the configured loop limit, and do not let lifecycle
  machinery make specialist decisions.
- Treat sub-slices as real child work items that go through the normal flow,
  reviews, and evidence capture.

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

Branch expectations:

- `main` is the stable branch.
- `develop` is the daily integration branch.
- Feature, fix, spike, and documentation branches should normally branch from
  `develop` and use the `codex/` prefix, for example
  `codex/project-build-boundary`.
- Commit focused progress on the active feature branch often enough that work
  can be reviewed, recovered, or promoted without reconstructing local state.
- Push the active feature branch after meaningful commits so GitHub is the
  shared source of truth for review and recovery.
- At the end of every feature, fix, spike, or documentation slice, promote the
  branch back to `develop` through a pull request, request GitHub Copilot review
  with `@copilot review`, address review feedback where appropriate, and merge
  only after the branch is ready for integration.
- Commit directly to `main` only when the user explicitly asks for it or when
  fast-forwarding a verified `develop` baseline back to `main`.

Before changing files:

- Run `git status --short --branch`.
- Notice uncommitted user work and avoid reverting it.
- Prefer `git mv` or normal filesystem moves for renames so history remains
  understandable.
- Keep generated runtime state, secrets, tokens, tenant-specific credential
  files, and local logs out of Git.

Before committing:

- Keep commits focused around one product or implementation slice.
- Review `git diff --stat` and the staged diff.
- Run `python scripts/check-pr-size.py --base origin/develop --committed-only` before pushing
  a feature branch. If it fails, split the work into smaller PRs before merge.
- Run `pytest -q` for code changes.
- Run `python -m agentic_mesh.cli validate-config` for config or project
  boundary changes.
- Run `docker compose ... config --quiet` when Compose outputs change.
- Update `MEMORY.md` when future agents need the context.
- Update ADRs or implementation-slice docs when a design decision or accepted
  boundary changes.

Commit hygiene:

- Use clear imperative commit messages, for example
  `Move Compose outputs under project folder`.
- Commit small, coherent units of work rather than holding broad local changes
  until the end of a long session.
- Push committed feature work before asking for review or handing the slice to
  another agent.
- Keep PRs reviewable: target one focused slice, prefer under 1,500 changed
  lines, and do not merge a PR over 5,000 changed lines or 50 files without a
  documented generated-artifact exception.
- Use PRs into `develop` for normal promotion. Do not bypass PR review for
  feature work unless the sponsor explicitly directs an emergency exception.
- Ask GitHub Copilot to review every normal feature PR before merging.
- If GitHub Copilot refuses review because the PR is too large, split the PR
  rather than merging it.
- Do not mix unrelated changes from the old `C:\Dev\dev-team-ai` prototype repo
  into this repository.
- Do not squash unrelated user changes into your commit.
- Prefer fast-forward merges between `develop` and `main` when possible so the
  local history stays easy to inspect.
