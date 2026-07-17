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
- `ADR-004`: V3 agent-owned runtime reset.
- `ADR-005`: Clean V5 replacement boundary.
- V4 remote-control runtime supersedes V3 for active dogfood/runtime work.

Core principles:

- V4 remains the active deployed runtime while V5 is built under
  `src/agentic_mesh_v5`. New product development follows the ordered V5
  backlog; V4 changes are limited to operational fixes needed before cutover.
- V5 must not import V4 runtime modules. Reusable V4 assets are explicitly
  classified, then ported, rewritten, referenced, or rejected in later V5
  stories.
- Do not add new runtime code under removed or legacy package paths. V2 and V3
  implementation packages and tests have been removed; no active deployment,
  prompt, Compose profile, or role workflow should depend on them or recreate
  them.
- V4 agents own work progression. The runtime provides platform services such
  as startup, hibernation, connector bridges, document-library access,
  reporting, config materialisation, telemetry, and reliable handoff delivery.
- Governance is explicit. Agents must consult required RACI roles and
  stakeholders before completing phases, inform roles that must be informed,
  and record governance exceptions when consultation is intentionally skipped.
- Runtime operational state and read models are stored in Postgres through the
  V4 database repository interface.
- The document library remains the canonical project knowledge base. Runtime
  database records make work inspectable, recoverable, and observable; they do
  not replace durable project documentation.
- Durable role-agent work is recorded through safe-output calls and terminal run
  status, not by parsing free-text or legacy JSON result envelopes. Ordinary
  conversation may stream directly from the role's remote-control Codex thread.
- Long-running self-contained role services are the operating model. Agents own
  role judgment, handoffs, consultations, documentation, and confirmation.
- Permanent role templates are stable and rarely updated.
- Projects apply project-specific overrides to role templates.
- A project can run multiple instances of the same role.
- The document library is canonical project memory. Role memory is a concise,
  source-linked accelerator and must cite documents, work items, or events.
- Source repos, deployed runtime installs, runtime state, and project repos
  must remain distinct unless a local-dev override is explicit.
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
  `example-project.engineering.1`

Current example files:

- `config/roles/product-manager.yaml`
- `config/roles/engineering.yaml`
- `examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml`
- `examples/projects/example-project/agentic-mesh/project.yaml`

These are starter examples, not final canonical role templates. Runtime code
must target the V4 remote-control runtime model.

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

- Keep all architecture and process as simple as possible unless the sponsor directly instructs otherwise. Use the existing runtime and workflow before introducing a new component, execution path, governance step, abstraction, or specialist handoff.
- Prefer small, testable slices.
- Prefer integrating mature existing libraries, tools, standards, and adapters
  for established capabilities before building a bespoke implementation. Build
  locally only when a suitable dependency does not exist, creates unacceptable
  product/security/licensing risk, or would break the intended adapter
  boundary.
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
- Do the concrete work through the appropriate tool or safe-output path before
  confirming completion. If no work is appropriate, record or state the no-op
  reason. Never say that an approval, handoff, document update, queue change,
  release, deployment, or closure has happened unless the corresponding system
  action has actually been recorded.
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

- `develop` is the daily development and integration branch.
- `main` is the stable release branch. It should move only when a tested,
  complete set of features is intentionally released.
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
- Do not commit directly to `main` during normal development.
- Do not commit directly to `develop` for normal feature work. Use a feature
  branch and merge it back through review unless the sponsor explicitly directs
  an operational exception.

Release flow:

- Release from `develop` to `main` only after the included features are
  complete, tested together, and ready to be treated as stable.
- Prefer a release PR from `develop` into `main` with a short release summary,
  test evidence, known risks, and rollback notes.
- Tag releases on `main` when a versioned release is cut.
- Do not cherry-pick feature work straight to `main` unless explicitly handling
  a release-blocking hotfix.

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
- Run `python -m agentic_mesh_v4.cli --project-config examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml status-json`
  for a basic V4 runtime read-model smoke after configuring
  `AGENTIC_MESH_DATABASE_URL` or the `AGENTIC_MESH_DATABASE_*` Postgres
  environment variables.
- For source/runtime/project boundary changes, add or update V4 topology tests
  and run the relevant `tests/test_v4_*.py` coverage. The legacy
  `validate-topology` command was removed with the V2/V3 packages.
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
- Prefer merge strategies that preserve a readable history for reviewed PRs.
- Use fast-forward only for local branch catch-up or release movement when it
  does not bypass the PR/review policy.
