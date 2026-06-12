# V2 Teams Connector Downstream Handoff

Status: ready for downstream shaping

Owner role: product-manager

Date: 2026-06-12

Source document:

- `docs/product/v2-teams-connector-product-definition.md`

## Purpose

This document hands the v2 Teams connector product definition to downstream
roles for UX, architecture, security, prompt-contract, implementation, QA, and
release shaping.

The live v2 role-service handoff mechanism is not yet running, so this is a
documented handoff package. When v2 role services are available, this package
should become the source material for a tracked work item.

## Product Outcome To Preserve

The connector should make Agentic Mesh feel like working with a human team of
specialists:

- humans can direct message specialist roles
- roles can message humans for decisions or clarification
- project channels provide shared context without noisy replies
- feature or epic channels focus larger discussions
- team-wide prompts invite relevant specialist input only
- durable work is proposed explicitly through safe-output tools
- important decisions move from Teams into the document library with provenance

Teams is the collaboration surface. The v2 runtime remains the orchestration,
state, approval, safe-output, and audit authority.

## Handoff Assignments

### UX Designer

Objective:

Design the human interaction experience for role DMs, project channels, feature
channels, team-wide relevance prompts, agent-initiated human questions, sponsor
questions, approvals, and status links.

Questions to answer:

- How should separate role-agent identities appear in Teams?
- What should a normal in-role DM reply look like?
- How should an agent-initiated question present context, requested decision,
  answer options, and links?
- How should channel messages avoid clutter while still making important
  blockers, approvals, and decisions visible?
- How should humans see whether a conversation became durable work?
- How should Markdown content be rendered without truncation or unreadable
  formatting?

Expected outputs:

- conversation scenario sketches or wire descriptions
- Teams message/card content patterns
- guidance for DM, channel, thread, and group-chat routing UX
- acceptance scenarios for human-team collaboration

### Solution Architect

Objective:

Design the connector and conversation architecture without making Teams the
orchestration engine.

Questions to answer:

- How should one connector runtime support separate role-agent identities?
- What is the runtime model for conversations, participants, Teams message ids,
  delivery records, thread binding, and idempotency?
- How do direct-message privacy, project-channel shared context, feature-channel
  context, and promoted work relate?
- How should safe-output tools route `status.reply`, `sponsor.ask_question`,
  proactive work proposals, handoffs, approvals, and delivery failures?
- How should this design generalise to Slack, GitHub Issues, Azure DevOps, and
  other input systems?

Expected outputs:

- connector architecture design
- database/read-model changes
- identity and routing model
- idempotency and delivery-failure model
- integration points with safe-output tools and role services

### Security Architect

Objective:

Define security, identity, privacy, retention, and audit requirements for
Teams conversation handling.

Questions to answer:

- What Teams and Entra permissions are needed for role identities, DMs, channel
  messages, mentions, cards, and delivery status?
- How are sponsors, humans, operators, and role agents authorised?
- How are private DMs protected until promoted into work?
- What retention defaults should apply to raw Teams messages, compacted
  summaries, and source-linked decisions?
- What audit evidence is required for enterprise use?

Expected outputs:

- permission and consent model
- privacy and retention requirements
- audit and data-classification requirements
- security risks and mitigations

### Prompt Engineer

Objective:

Design role instructions and behavioural tests for the human-team interaction
model.

Questions to answer:

- How should prompts tell roles when to answer conversationally, ask a human,
  propose work, hand off, consult, or stay silent?
- How should every role perform lightweight relevance checks for team-wide
  prompts?
- How should prompts prevent fake claims about messages, work items, approvals,
  releases, or deployments?
- How should role instructions guide DM versus channel versus group-chat
  communication without rigidly enforcing one style?

Expected outputs:

- prompt-contract guidance
- relevance-check instruction pattern
- agent-initiated question instruction pattern
- regression scenarios for invalid/noisy conversation behaviour

### Engineering

Objective:

Plan and implement the connector only after UX, solution architecture, security,
and prompt-contract assumptions are clear enough.

Questions to answer:

- What is the smallest useful Teams connector MVP?
- Which database tables, safe-output handlers, role-service hooks, and delivery
  adapters are required first?
- How will duplicate Teams events be handled?
- How will role identities be configured and deployed?
- How will connector status be visible in the v2 dashboard?

Expected outputs:

- implementation plan
- code changes
- focused tests
- configuration examples
- deployment instructions

### QA Engineer

Objective:

Define BDD and regression coverage for the Teams connector behaviour before and
after implementation.

Questions to answer:

- What scenarios prove DMs do not create work by default?
- What scenarios prove channel context is captured without waking every role?
- What scenarios prove relevance checks avoid noisy team-wide replies?
- What scenarios prove agent-initiated questions are delivered and bound back
  to the originating work?
- What scenarios prove idempotency, delivery failure reporting, and permission
  failures?

Expected outputs:

- BDD acceptance scenarios
- connector integration test plan
- failure-mode test plan
- evidence requirements for release

### Release Manager

Objective:

Define how the connector is deployed, validated, rolled back, and closed as
released or no-deployment.

Questions to answer:

- What deployment target installs the connector and role identities?
- How is tenant/app registration state validated?
- What smoke tests prove inbound, outbound, DM, channel, and approval routing?
- What rollback or disablement path is available if the connector misroutes or
  spams?

Expected outputs:

- deployment and rollback plan
- smoke test checklist
- release evidence requirements

## Suggested Slice Sequence

1. UX and Solution Architecture shape the interaction and connector model in
   parallel.
2. Security Architecture reviews identity, privacy, and retention before
   Engineering starts.
3. Prompt Engineer reviews role communication instructions and relevance-check
   guidance.
4. QA writes BDD scenarios from the agreed interaction and architecture model.
5. Engineering implements the smallest connector MVP.
6. Release Manager deploys, smoke tests, records release evidence, and closes
   the work.

## Open Decisions

- Confirm Teams implementation approach for separate role-agent identities:
  separate bot/app registrations, one app with role-addressable surfaces, or a
  hybrid approach.
- Choose default retention values for raw Teams messages, compacted summaries,
  and durable decision records.
- Decide whether feature/epic channels are manually configured first or created
  by the runtime later.

## Review Log

- RL-001 | product-manager | downstream-handoff | full document | Created
  handoff package for UX, Solution Architecture, Security Architecture, Prompt
  Engineering, Engineering, QA, and Release Management. | incorporated
  2026-06-12
