# V2 Teams Connector Interaction Design

Status: UX downstream design

Owner role: UX Designer

Date: 2026-06-12

Source documents:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`

## Purpose

This document defines the human interaction design for the v2 Microsoft Teams
connector. It covers how sponsors, collaborators, operators, and role agents
interact through Teams while the v2 runtime remains the authority for routing,
state, queues, approvals, safe-output calls, audit, and durable work.

The design goal is an enterprise collaboration experience where configured
role agents are visible, addressable specialists in Teams without turning
Teams into an agent-to-agent bus or a hidden workflow engine.

## Design Principles

- Teams is the human collaboration surface, not the orchestration engine.
- Role agents should be recognizable as separate specialist identities.
- Direct messages remain conversational and private until explicitly promoted.
- Project channels provide shared context without forcing every role to reply.
- Feature, epic, incident, or initiative channels narrow context for larger
  discussion areas.
- Team-wide requests invite relevance-checked specialist input, not blanket
  status reports.
- Important decisions, requirements, risks, approvals, and release facts must
  be linked back to durable project records.
- Agent-visible consults, handoffs, blockers, and lifecycle progression happen
  through runtime tools, not Teams mentions between agents.
- Human-facing messages must be concise, complete, and understandable without
  exposing raw runtime JSON.

## Teams Surfaces

### Direct Message

A direct message is the preferred surface for focused conversation between one
human and one role agent.

Use DMs when:

- a sponsor asks a role-specific question
- a role needs focused clarification from one accountable person
- the question may be missed in a busy channel
- the topic is private until promoted into tracked work or durable knowledge
- the role can answer without needing visible group deliberation

Expected UX:

- The role replies in the same DM conversation.
- Ordinary conversational replies do not create work items.
- If the conversation is promoted, the reply explains what was created or
  proposed and links to the work item, queue item, approval, or status page.
- The reply should not include low-value acknowledgements unless work becomes
  long-running, blocked, or waiting on the human.

### Project Channel

The default project channel is the shared project room.

Use the project channel when:

- a human wants shared project context visible to the team
- a role is mentioned for focused visible input
- a status, blocker, approval, or decision should be visible to project
  collaborators
- an agent asks a question that needs project-level discussion

Expected UX:

- Unmentioned messages are captured as shared context and do not wake every
  role by default.
- Mentioned role messages route to the addressed role.
- Other roles stay quiet unless consulted through runtime tools, handed off to,
  or included by a team-wide trigger.
- Important outcomes should be summarized with source links into the document
  library, work item, decision record, risk, or release evidence.

### Feature Or Initiative Channel

Feature, epic, incident, or initiative channels are focused project rooms.

Use focused channels when:

- the discussion is long-running enough to clutter the default project channel
- the audience differs from the broader project team
- the work has its own risks, approvals, or delivery cadence
- role relevance should be evaluated against a narrower context

Expected UX:

- The channel should preserve the same role mention, thread binding, approval,
  and status-link behavior as the default project channel.
- Durable outcomes still land in canonical project artifacts, not only in the
  channel history.
- Channel naming and creation policy may be manual in the MVP.

### Thread

Threads keep a conversation bound to an originating message, work item,
question, approval, or decision.

Use threads when:

- replying to an agent question
- discussing an approval or risk acceptance request
- continuing a role response in a channel
- keeping status updates attached to their original work context

Expected UX:

- A thread reply remains part of the same conversation context unless the human
  explicitly starts a new work item.
- Status and approval messages should prefer threads so channels stay scannable.
- Final outcomes should include a link to the durable record when one exists.

### Group Chat

Group chats support bounded human discussion when a channel is too broad and a
DM is too narrow.

Use group chats when:

- a role needs input from multiple named humans
- a sponsor intentionally loops in another person
- a decision should include a small accountable group without posting to a
  project channel
- a human and one or more roles need a short visible discussion

Expected UX:

- The initiating message names the requested decision or clarification.
- Required participants are mentioned where Teams supports it.
- The conversation remains linked to the originating work item, queue item,
  approval, risk, or document context.
- If another role is needed for specialist input, the role uses runtime consult
  or handoff tools. Teams group chat may show a human-visible summary, but it
  must not be the agent-to-agent transport.

## Role Identity UX

Each configured role should appear in Teams as an addressable specialist,
subject to the selected Teams implementation model.

Role identity should expose:

- display name, such as `AM-Product Manager` or project-configured equivalent
- role title, such as Product Manager, Engineering, QA Engineer, or Release
  Manager
- project association when needed to disambiguate the same role across teams
- optional instance identifier only when operationally relevant
- consistent avatar or badge style that identifies the role as an Agentic Mesh
  role, not a human employee

Role identity should avoid:

- names that imply the agent is a specific human
- hidden gateway identities that make all role replies look interchangeable
- exposing internal container ids, database ids, queue ids, or worker-provider
  names in normal human conversation
- relying on Teams bot-to-bot mentions for role collaboration

When multiple instances of the same role exist, humans normally address the
role rather than a specific instance. The runtime chooses the handling instance
and may show the instance id only in operator-facing diagnostics or status
links.

## Message Patterns

### Normal Role Reply

Use for conversational answers in DMs or focused channel mentions.

Pattern:

```text
<direct answer>

<brief supporting context or caveat when needed>

<optional next step or link when the reply created or references durable work>
```

Requirements:

- Send one complete Markdown reply.
- Do not truncate long answers artificially.
- Do not expose raw JSON, tool payloads, queue envelopes, or stack traces.
- Keep the role voice factual and accountable.
- Link to source artifacts when citing durable project facts.

### Work Proposed From Conversation

Use when a role determines that a Teams conversation should become durable
tracked work.

Pattern:

```text
I think this should become tracked work because <value or risk rationale>.

Proposed work: <short title>
Owner role: <role>
Source: <conversation/thread link>
Status: <proposed or created>

<work item or queue item link>
```

Requirements:

- The durable state change must come from safe-output tools.
- The Teams reply reports the result and source link.
- If the role only proposes work, the message should make the approval or next
  human action clear.

### Agent-Initiated Question

Use when a role needs human clarification, decision, approval, risk acceptance,
or missing context.

Pattern:

```text
I need your input on <work or decision>.

Context: <one to three concise facts>
Requested response: <specific question or decision>
Response needed by: <date/time or "before <gate>" when applicable>

<answer options, if structured>
<status or artifact link>
```

Requirements:

- Put the requested response before background detail.
- Use DM for focused input from one accountable person.
- Use channel, group chat, or thread when visible discussion is needed.
- Mention required humans or roles in shared surfaces.
- Preserve the link back to the originating work item, queue item, approval,
  risk, or document context.

### Approval Or Gate Card

Use Adaptive Cards for structured approvals, risk acceptance, release gates,
and other response contracts.

Card content should include:

- approval title
- requesting role
- project and work item
- decision requested
- options, such as approve or not approve
- required comment field when policy requires rationale
- due date or blocking gate
- links to relevant evidence, docs, status, or release record
- privacy and audit note when the response will be stored durably

Card behavior should:

- submit a normalized response to the runtime
- update the original card with the captured decision and timestamp
- keep the card in the originating thread when possible
- show delivery or submission failure visibly
- avoid requiring the human to inspect runtime internals to act

### Status Update

Use status updates for meaningful state transitions, not routine noise.

Post status updates when:

- long-running work starts or materially progresses
- work is blocked
- a role is waiting for human input
- an approval or lifecycle gate is reached
- work is completed, failed, or handed to the next visible stage

Pattern:

```text
Status: <state>
Work: <short title>
Role: <role>
Reason: <brief explanation>
Next: <owner or next action>

<status/work item link>
```

Requirements:

- Prefer posting in the existing thread for the work.
- Use project or focused channels for visible work status.
- Use DMs only when the update concerns a private conversation or a specific
  accountable human.

### Delivery Failure

Use when the connector cannot deliver a message, card, or reply.

Pattern:

```text
Delivery failed: <message type>
Target: <surface or participant>
Impact: <what may be delayed or blocked>
Next action: <operator or retry action>

<dashboard/status link>
```

Requirements:

- Record the failure in the runtime delivery model.
- Surface the failure in the dashboard.
- Avoid repeated channel spam for retry loops.

## Status And Approval Link UX

Runtime links should help humans move from Teams to durable context without
exposing implementation details.

Link labels should be human-readable:

- `View work item`
- `Review approval`
- `Open status`
- `View decision record`
- `View release evidence`
- `Open source conversation`

Links should resolve through the configured external base URL and include only
stable identifiers required by the status or artifact view. Teams messages
should not expose local filesystem paths, raw database keys, secret names, or
provider-specific worker ids.

Approval links and cards should preserve:

- correlation id
- source Teams conversation and thread
- requesting role
- target human or group
- work item or gate
- submitted response and timestamp
- delivery status

When a card action succeeds, the Teams surface should visibly update so other
viewers do not act on a stale approval. When a card action fails, the human
should see a clear failure state and a status link for recovery.

## Markdown And Full-Reply Expectations

Agent replies should use Teams-compatible Markdown as the default for complete
role-authored messages.

Expected formatting:

- short paragraphs
- plain bullets for lists
- numbered steps only when order matters
- fenced code blocks for commands, snippets, or structured examples
- inline links with descriptive labels
- tables only for compact comparisons that remain readable on mobile

Avoid:

- artificial truncation with "continued..." fragments
- raw JSON as the primary user-facing answer
- deeply nested bullets
- overly wide tables
- large pasted logs
- Markdown that relies on renderer-specific behavior
- hidden meaning conveyed only by emoji, color, or iconography

For long answers, the role may provide a concise Teams reply with a link to a
durable artifact, but the Teams reply must still answer the user's immediate
question or explain where the complete answer was recorded.

## Accessibility Considerations

Teams messages and Adaptive Cards must remain usable with keyboard navigation,
screen readers, high contrast modes, and small mobile screens.

Requirements:

- Use descriptive link text rather than bare URLs.
- Do not rely on color alone to communicate status or risk.
- Keep card titles and button labels specific.
- Put the requested human action near the top of cards and questions.
- Use concise field labels in cards.
- Keep answer options mutually exclusive where the response contract requires
  one selection.
- Avoid dense tables in cards.
- Include text equivalents for status icons or badges.
- Ensure role names are readable without relying only on avatar differences.
- Keep messages scannable when Teams collapses previews or notifications.

## Noise And Relevance Guidance

The connector experience should minimize avoidable channel noise.

Default behavior:

- DM questions receive one complete role reply.
- Unmentioned project-channel messages are captured as shared context without
  role replies by default.
- A role mention routes to the mentioned role only.
- Team-wide triggers run relevance checks before role replies are posted.
- Low-relevance roles stay quiet.
- Routine "I received this" acknowledgements are suppressed.

Explicit progress messages are appropriate when:

- work will take materially longer than a conversational response
- a lifecycle gate is reached
- human input is required
- work is blocked or failed
- the role has created or proposed durable work

## Human-Visible Versus Internal Agent Activity

Human-visible Teams activity should include:

- direct role answers
- agent-initiated human questions
- sponsor questions and answers
- approval and gate cards
- meaningful status updates
- blocker summaries
- links to work, decisions, evidence, and release records

Internal runtime activity should not be represented as Teams conversation unless
there is a human reason to show it. This includes:

- agent-to-agent consult routing
- handoff mechanics
- queue claim details
- relevance scoring details
- worker adapter payloads
- retry envelopes
- database implementation details

When an internal consult or handoff produces a human-relevant outcome, the role
may post a concise summary in the appropriate thread or channel with links to
the durable record.

## Acceptance Scenarios

### DM Question Does Not Create Work

Given a sponsor DMs the Product Manager with a product question, when the role
can answer conversationally, then the sponsor receives exactly one complete
Markdown reply and no work item is created.

### DM Feature Request Becomes Proposed Work

Given a sponsor DMs the Product Manager asking for a feature, when the role
determines the request should become durable work, then the role uses
safe-output tools to propose or create the work and replies with rationale,
source link, and work/status link.

### Unmentioned Channel Message Is Context Only

Given a human posts an unmentioned note in the project channel, when the
connector ingests the message, then the message is stored as shared context and
does not wake every role or generate role replies.

### Mentioned Role Replies Without Channel Flooding

Given a human mentions Engineering in the project channel, when the connector
routes the message, then Engineering may reply in the thread and other roles do
not reply unless runtime consult, handoff, or a team-wide trigger includes them.

### Team-Wide Trigger Uses Relevance Checks

Given a human asks the whole role mesh for input, when relevance checks run,
then only roles with material specialist input reply and non-relevant roles
remain silent while the runtime records relevance decisions.

### Agent Asks Focused Human Question

Given QA needs a sponsor clarification before completing a gate, when QA sends
an agent-initiated question by DM, then the message states the work context,
requested answer, due point, and status link, and the reply binds back to the
originating work item.

### Agent Starts Broader Decision Thread

Given Release Management needs visible risk acceptance from multiple humans,
when the role posts to a channel or group chat, then required participants are
mentioned, the decision request is clear, and the thread remains bound to the
originating approval or risk record.

### Approval Card Captures Durable Response

Given a sponsor receives an approval card in Teams, when the sponsor submits a
decision, then the card updates with the decision state and the runtime stores
the normalized response against the correct gate.

### Human Loops In Another Participant

Given a human adds another person to an agent-initiated discussion, when the
connector processes the added conversation, then the discussion remains
auditable and the final decision can still be linked to the originating work or
document record.

### Markdown Reply Remains Readable

Given a role sends a long answer with commands or references, when Teams renders
the reply, then the answer uses readable Markdown, descriptive links, and code
blocks where needed without truncating the substantive response.

### Delivery Failure Is Visible

Given a Teams send operation fails, when the runtime records the delivery
failure, then operators can see the failed delivery in status surfaces and the
human-facing channel is not spammed by repeated retry notices.

### Duplicate Teams Event Is Idempotent

Given the same Teams event is received twice, when the connector ingests it,
then duplicate replies, work items, approvals, and delivery records are not
created.

## Open UX Questions

- What exact display-name convention should be used when the same role appears
  in multiple project Teams?
- Should the MVP show role-instance ids anywhere outside operator diagnostics?
- Which approval response templates need Adaptive Card designs first?
- What card update behavior is feasible across Teams clients and tenant
  policies?
- What raw Teams message retention copy should be shown to enterprise users
  when a DM is promoted into durable work?

## Review Log

- RL-001 | UX Designer | downstream-review | full document | Created Teams
  connector interaction design from the product definition and downstream
  handoff. | incorporated 2026-06-12
- RL-002 | UX Designer | product-alignment | Teams as collaboration surface |
  Confirmed that visible Teams UX must not become agent-to-agent orchestration;
  consults, handoffs, and durable state changes remain runtime-owned. |
  incorporated 2026-06-12
- RL-003 | UX Designer | acceptance-coverage | human interaction scenarios |
  Added acceptance scenarios for DMs, channel context, role mentions,
  team-wide relevance, agent-initiated questions, approvals, Markdown replies,
  delivery failures, and idempotency. | incorporated 2026-06-12
