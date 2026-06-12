# Product Backlog

Status: initial

Owner role: product-manager

Date: 2026-06-12

## Purpose

This document captures product backlog candidates that should be shaped into
future slices, spikes, or features. It is not the runtime work queue. Items here
need Product Manager review before they become tracked delivery work.

## Backlog Items

### PB-001 Prompt Engineer Review Of Prompt Materials

Status: backlog

Type: spike / quality improvement

Owner role: prompt-engineer

Supporting roles:

- product-manager
- qa-engineer
- engineering
- ux-designer

Intent:

Once Agentic Mesh has captured the intended role behaviour, human interaction
model, safe-output contract, and v2 runtime boundaries, the Prompt Engineer
should review all prompt creation materials, starter prompts, role charters,
safe-output instructions, context-loading guidance, and behavioural tests
against that intent.

Rationale:

The role-agent experience depends on prompts that make agents act like
accountable specialists rather than generic chatbots or brittle JSON emitters.
This review should happen after intent is documented well enough to test prompts
against it.

Acceptance criteria:

- Review covers global prompt components, starter role templates, safe-output
  instructions, conversation guidance, lifecycle guidance, and prompt trace
  audit expectations.
- Review identifies contradictions, missing tool guidance, over-prescriptive
  wording, missing human-collaboration behaviours, and unsafe claims agents
  might make.
- Output includes recommended prompt-contract changes and regression scenarios.
- No prompt rewrite begins until Product Manager confirms the target intent is
  sufficiently documented.

### PB-002 Human-Team Collaboration Experience Validation

Status: backlog

Type: product / UX validation

Owner role: product-manager

Supporting roles:

- ux-designer
- prompt-engineer
- solution-architect
- qa-engineer

Intent:

Validate that Agentic Mesh interactions feel like collaborating with a human
team of specialists: direct questions, team-room discussions, focused mentions,
agent-initiated human questions, proactive work proposals, and documented
decisions.

Rationale:

The product should not feel like managing a set of disconnected automation
scripts. Humans and agents should be able to cooperate, discuss, ask questions,
escalate, and create durable work in ways that mirror a capable human project
team.

Acceptance criteria:

- Define representative conversation scenarios for direct messages, project
  channels, feature channels, team-wide prompts, and agent-initiated questions.
- Identify which behaviours belong in role instructions, connector UX, runtime
  state, safe-output tools, or dashboard visibility.
- Confirm that important decisions move from conversation into the document
  library with provenance.
- Produce UX and QA scenarios before Teams connector implementation begins.

### PB-003 V2 Teams Connector Downstream Design And Build

Status: downstream drafts produced

Type: feature

Owner role: product-manager

Supporting roles:

- ux-designer
- solution-architect
- security-architect
- prompt-engineer
- engineering
- qa-engineer
- release-manager

Intent:

Progress the sponsor-shaped Teams connector product definition through
downstream UX, architecture, security, prompt-contract, implementation, QA, and
release work.

Rationale:

The Teams connector is the first major human-collaboration surface for v2. It
must support direct role DMs, project-channel shared context, team-wide
relevance checks, agent-initiated human questions, proactive work proposals, and
durable document-library outcomes without repeating v1's noisy channel and
hidden-state failures.

Source artifacts:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`
- `docs/ux/v2-teams-connector-interaction-design.md`
- `docs/architecture/v2-teams-connector-architecture.md`
- `docs/security/v2-teams-connector-security.md`
- `docs/prompt-engineering/v2-teams-connector-behaviour-guidance.md`
- `docs/qa/v2-teams-connector-test-plan.md`

Acceptance criteria:

- UX defines DM, channel, thread, approval, status-link, and agent-initiated
  question patterns. Initial draft produced in
  `docs/ux/v2-teams-connector-interaction-design.md`.
- Solution Architecture defines connector runtime, identity, conversation,
  routing, idempotency, and safe-output integration. Initial draft produced in
  `docs/architecture/v2-teams-connector-architecture.md`.
- Security Architecture defines Teams/Entra permissions, privacy, retention,
  and audit requirements. Initial draft produced in
  `docs/security/v2-teams-connector-security.md`.
- Prompt Engineer defines role communication instructions, relevance-check
  prompts, and invalid-output regression scenarios. Initial draft produced in
  `docs/prompt-engineering/v2-teams-connector-behaviour-guidance.md`.
- QA defines BDD and failure-mode coverage before implementation. Initial draft
  produced in `docs/qa/v2-teams-connector-test-plan.md`.
- Engineering implements the smallest useful MVP after the shaping work is
  clear.
- Release Manager deploys, smoke tests, records release evidence, and closes the
  work only when deployment or no-deployment disposition is explicit.

### PB-004 Runtime Downstream Role Execution

Status: backlog

Type: runtime feature

Owner role: delivery-manager

Supporting roles:

- solution-architect
- engineering
- qa-engineer
- prompt-engineer

Intent:

Implement the v2 runtime mechanism that turns a product handoff package or
queue/work item into role-service assignments that downstream role agents can
claim, execute, report on, and hand off through safe-output tools.

Rationale:

The current v2 reset branch can document downstream assignments, but it cannot
yet run live downstream role agents from those assignments. Until this exists,
handoffs to UX, Solution Architecture, Security, Prompt Engineering,
Engineering, QA, and Release Management must be performed manually or by
external Codex subagents, which does not prove Agentic Mesh itself.

Acceptance criteria:

- A work item can create role-specific assignments from a documented handoff.
- Long-running role services can claim assignments from the database.
- Role assignments include source documents, current flow state, allowed
  safe-output tools, target outputs, and context visibility.
- Completion, blocker, consult, handoff, sponsor question, and no-op outcomes
  are recorded through safe-output calls.
- The dashboard shows current role assignments and terminal outcomes.
- Tests prove downstream handoff execution without using Teams as
  agent-to-agent transport.

### PB-005 Containerised Role-Agent Hibernation And Hydration

Status: backlog

Type: runtime feature

Owner role: platform-engineer

Supporting roles:

- solution-architect
- engineering
- qa-engineer
- security-architect
- release-manager

Intent:

Support full long-running containerised role agents while allowing idle
role-service containers to hibernate after a configurable idle period and
hydrate later into the same logical role-instance state.

Rationale:

Agentic Mesh should be able to run many role agents without permanently paying
for idle containers. However, hibernation must not lose role identity,
assignments, memory, prompt traces, connector cursors, work-item state, or
safe-output history. The system should restore logical state from durable
runtime records rather than depending on fragile process-memory snapshots.

Acceptance criteria:

- Each role instance records enough durable state to stop and restart without
  losing identity, assignments, memory, context, or recovery information.
- Idle detection is configurable per project, role, or deployment profile.
- Hibernation only occurs at safe points: no active tool call, no unflushed
  safe-output record, no unrecoverable worker subprocess, and no uncommitted
  file operation.
- Hydration reloads role memory, pending assignments, conversation context,
  connector cursors, prompt traces, safe-output history, and worker adapter
  configuration.
- Active long-running work is protected by leases, heartbeats, cooperative
  suspend, retry, or explicit "do not hibernate" policy.
- The dashboard shows role instances as active, idle, hibernating, hibernated,
  hydrating, blocked, or failed.
- Tests cover idle hibernation, wake on new work, wake on human message, wake on
  scheduled retry, crash recovery, and no-hibernate-during-active-tool-call.
- Process checkpoint/restore is documented as optional future research, not the
  default v2 baseline.

## Review Log

- RL-001 | product-manager | initial | full document | Created initial backlog
  with prompt-engineering review and human-team collaboration validation as
  future work. | incorporated 2026-06-12
- RL-002 | product-manager | backlog-update | PB-003 | Added Teams connector
  downstream design and build item from sponsor request to hand the product
  definition to UX, Solution Architecture, and other roles. | incorporated
  2026-06-12
- RL-003 | product-manager | backlog-update | PB-004 | Added runtime downstream
  role execution gap discovered while trying to progress the Teams connector
  handoff before live v2 role services exist. | incorporated 2026-06-12
- RL-004 | product-manager | backlog-update | PB-005 | Added long-running
  containerised role-agent hibernation and hydration feature from sponsor
  question about saving resources while preserving role state. | incorporated
  2026-06-12
