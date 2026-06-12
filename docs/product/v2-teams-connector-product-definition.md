# V2 Teams Connector Product Definition

Status: sponsor-shaped product definition

Owner role: product-manager

Date: 2026-06-12

Source: sponsor request to design the new Teams connector for the v2 runtime.

## Sponsor Decisions

The sponsor provided product direction on 2026-06-12:

- Role agents should be visible as separate agent/role identities in Teams.
- Direct messages to a role are private to that human and role until promoted
  into tracked work or summarized into a durable project artifact.
- Project-channel messages are shared project context. They may be compacted,
  but important points, decisions, risks, and instructions must be preserved.
- Team-wide requests should reach every role for a lightweight relevance check.
  A role should respond only when its relevance score and specialist judgment
  indicate it has something important to add.
- A project should have a Project Team with a default `project` channel. Larger
  features, epics, or focused initiatives may create additional channels so
  discussion and relevance stay focused.
- Roles may proactively propose work from conversation when they can make a
  good case for necessity and value.
- Teams conversations inform, consult, and keep humans and agents updated. The
  document library remains the primary source of truth for important decisions,
  architecture, requirements, risks, and release records.
- Agents must be able to initiate human conversations when they need
  clarification, decisions, approvals, risk acceptance, or specialist input from
  people.

## Product Intent

The v2 Teams connector should make Agentic Mesh feel like talking to a human
team of specialists.

Sponsors and collaborators should be able to direct message a role agent, post
in a project development channel, mention a specific role for focused input, or
ask the whole role mesh to inspect something without causing every role to spam
the channel.

Teams is the human collaboration surface. It must not become the orchestration
engine. Runtime state, routing, queue capture, safe-output calls, approvals,
handoffs, and release evidence remain owned by the v2 runtime.

## Product Manager Framing

The Product Manager role owns product intent, scope, non-goals, acceptance
criteria, downstream readiness, and sponsor clarification. This definition is
therefore intentionally focused on user outcomes and boundaries before
Engineering or Architecture choose the exact connector implementation.

The design must preserve these product qualities:

- Human users can speak naturally to role specialists.
- Conversational messages stay conversational unless a role or policy promotes
  them into durable work.
- Durable work is created only through explicit safe-output intent.
- Channel discussions become useful context without generating unnecessary
  replies.
- Important decisions and durable knowledge move from Teams conversation into
  the document library.
- Sponsor questions and approvals are visible, actionable, and routed back to
  the correct conversation.
- Agent-initiated questions use the most appropriate Teams communication style:
  direct messages for focused clarification, and channels or group chats for
  decisions that need broader human or agent discussion.
- Agent replies are concise, role-authored, Markdown-formatted, and complete.

## Target Users

- Sponsor or project owner who wants to steer work, ask questions, approve
  releases, and inspect progress without learning internal runtime mechanics.
- Specialist role agent that needs a clear conversation context, role identity,
  source channel, and permitted safe-output actions.
- Delivery or release owner who needs Teams conversations to stay linked to
  queue items, work items, approvals, blockers, and release records.
- Enterprise operator who needs connector behavior to be auditable, configurable,
  and compatible with tenant security controls.

## User Outcomes

- I can direct message any configured role agent and receive an in-role answer.
- I can ask a role agent to turn a conversation into tracked work, and the role
  can propose or create the right queue/work item through safe-output tools.
- I can post in the project development channel and have the message captured as
  shared project context.
- I can mention one role in the project channel and have that role respond,
  while other roles can still use the message as context later.
- I can address the full team and get only relevant specialist responses.
- I can answer sponsor questions, approve gates, or follow status links from the
  same Teams conversation where the work began.
- I can see enough context in Teams messages to understand what happened without
  reading raw JSON or internal logs.

## Interaction Model

### Direct Message To A Role

A direct message to a configured role agent is a conversation by default.

The runtime should create or continue a conversation record with:

- connector: Teams
- source type: direct message
- human participant identity
- role identity
- Teams conversation and message references
- visibility scope
- correlation id

The role agent receives recent conversation context, role memory, project
context, and the current prompt contract. It may respond with `status.reply`,
ask a sponsor question, propose durable work, or report that it cannot answer.

No queue item or work item should be created merely because a direct message was
sent.

Direct messages are private to the human and the addressed role until the role
or human explicitly promotes the conversation into tracked work, records a
source-linked memory update, or writes a durable document update.

### Project Team And Channels

Each project may configure a Project Team. The default channel should be
`project`, because the same organization may run multiple project teams with
different human membership, governance needs, and confidentiality boundaries.

Large features, epics, incidents, or focused initiatives may create additional
channels under the same project team. Those channels should narrow context and
improve relevance without changing the canonical project identity.

### Project Channel Context

Each configured project channel is a project room. A message in a project room
is project context by default.

Unmentioned channel messages should be stored as shared context and made
available to future role runs according to project retention and visibility
rules. They should not automatically wake every role.

The connector may compact retained channel history, but must preserve important
points, decisions, requirements, constraints, risks, and instructions with
provenance. Durable decisions should be written into the document library rather
than left only in Teams.

### Mentioned Role In Channel

When a human mentions a configured role in the project channel, the connector
should route the message to that role as a focused conversation assignment.

Other roles should not respond unless explicitly consulted, handed off to, or
included by a relevant team-wide trigger. The original channel message remains
available as shared context.

### Team-Wide Discussion

When a human addresses the configured team-wide trigger, every role should
perform a lightweight relevance check before role replies are posted.

Relevant roles may answer, ask clarifying questions, propose work, consult
another role, or no-op. Non-relevant roles should stay quiet, while the runtime
records enough audit data to explain that relevance was considered.

The runtime should support a configurable relevance threshold. A role may still
respond below the threshold when it can justify that it has important specialist
input, but the reason should be recorded.

The product experience should be closer to asking a meeting room "does anyone
have anything to add?" than instructing every person to produce a report.

### Thread Binding

Replies in a Teams thread should remain bound to the original conversation,
queue item, work item, sponsor question, or approval request where applicable.

Thread replies must be processed as part of the same conversational context
unless the human explicitly starts new work.

### Agent-Initiated Human Questions

Any role agent may need to ask a human for clarification, a product decision,
approval, risk acceptance, missing context, or specialist judgement. The Teams
connector must support this as a first-class conversation pattern.

The communication style should be role guidance rather than hard enforcement by
the connector:

- Use direct messages for focused questions that need one accountable human to
  answer and might be missed in a busy channel.
- Use a suitable channel, group chat, or thread when the question needs visible
  discussion between multiple humans, multiple agents, or a project group.
- Mention the specific humans or agents whose input is needed when using a
  channel or group discussion.
- Allow the human to loop in another human or agent, turning a direct
  clarification into a broader discussion when that is the natural way to reach
  the decision.
- Preserve the conversation link and final decision back to the originating
  work item, queue item, approval, risk, or document update.

The runtime should record the agent's intended audience, reason, source work
context, and delivery channel, but it should not prevent a role from choosing an
unusual communication route when the role can justify it.

## Scope

V2 Teams connector MVP scope:

- Teams inbound direct messages to configured role identities.
- Teams inbound project-channel messages.
- Teams inbound role mentions and configured team-wide triggers.
- Project Team configuration with a default `project` channel and optional
  feature, epic, or focused-work channels.
- Conversation records in the v2 database.
- Role reply routing through `status.reply`.
- Sponsor question routing from `sponsor.ask_question`.
- Agent-initiated human question routing from any role to a sponsor, named
  human, configured human group, role, channel, or thread.
- Queue/work proposal from conversation through safe-output tools.
- Teams delivery records and visible delivery failures.
- Markdown rendering for full agent replies.
- Project-channel context capture.
- Conversation compaction into source-linked context summaries.
- Runtime status links using the configured external base URL.

## Non-Goals

- Do not resurrect v1 Teams listener behavior.
- Do not use Teams bot-to-bot mentions as an orchestration mechanism.
- Do not create work items automatically for every Teams message.
- Do not make every role reply to every channel message.
- Do not rely on JSON final responses for normal conversation output.
- Do not hide product, approval, blocker, or release questions inside artifacts
  only.
- Do not require Teams as the only collaboration connector; the model must
  generalize to Slack, GitHub Issues, Azure DevOps, and other inputs.

## Functional Requirements

- Configure Teams connectors per project, including channel bindings, allowed
  users or groups, role aliases, and team-wide triggers.
- Configure a Project Team, default project channel, and optional feature or
  epic channels.
- Map Teams users to runtime people, sponsor candidates, and operator authority
  where configured.
- Map Teams role identities or aliases to v2 role services.
- Store inbound Teams messages as conversation events with source metadata.
- Route direct messages to the addressed role without creating a work item by
  default.
- Keep direct-message context private to the sender and role until it is
  explicitly promoted into work or durable project knowledge.
- Route mentioned channel messages to the mentioned role while preserving shared
  context.
- Run lightweight relevance checks for every role on all-team requests.
- Apply a configurable relevance threshold and record relevance decisions.
- Send complete Markdown replies through Teams with no artificial truncation.
- Suppress low-value acknowledgement and "started" messages for ordinary direct
  conversations.
- Send explicit progress messages only when work is long-running, blocked,
  waiting for human input, or crossing a lifecycle gate.
- Bind sponsor replies to the original question, approval, or work item.
- Bind agent-initiated human questions and replies to the original work item,
  queue item, approval, risk, or document context.
- Record connector delivery status, failures, retries, and final message ids.
- Ensure every durable state change comes from safe-output calls, not inferred
  connector text.
- Allow roles to proactively propose work from conversation when they can record
  a value-based rationale.
- Compact channel history while preserving source-linked important decisions,
  risks, requirements, and instructions in the document library or role memory.

## Acceptance Criteria

- Given a sponsor DMs the Product Manager with a question, when the role can
  answer conversationally, then Teams receives exactly one complete Markdown
  reply and no work item is created.
- Given a sponsor DMs the Product Manager asking for a feature to be built, when
  the role decides it is durable work, then a queue item or work item is proposed
  through safe-output tools and the Teams reply links to it.
- Given a sponsor posts an unmentioned note in the project development channel,
  when later role work needs relevant context, then that note can be discovered
  from the conversation store.
- Given a sponsor mentions one role in the project channel, when the message is
  processed, then only that role is assigned to respond unless it explicitly
  consults or hands off to another role.
- Given a sponsor uses the team-wide trigger, when relevance is evaluated, then
  only roles with material input respond.
- Given a role determines from conversation that durable work is necessary, when
  it can explain the value and urgency, then it may propose a queue/work item
  and reply with the rationale and link.
- Given a channel discussion contains an important decision, when the connector
  compacts or expires conversation history, then the decision remains preserved
  in the document library or source-linked memory.
- Given a sponsor replies in a Teams thread to a question or approval, when the
  connector processes the reply, then the runtime binds it to the original
  conversation/work item and continues the correct flow.
- Given an agent needs focused clarification from the sponsor, when the role
  chooses direct-message routing, then the sponsor receives a DM with the
  question, context, requested decision, and link back to the originating work.
- Given an agent needs a broader decision discussion, when the role chooses a
  channel or group route, then the message mentions the required people or roles
  and the resulting thread remains bound to the originating work.
- Given a human loops another person or agent into an agent-initiated
  conversation, when the connector processes the added discussion, then the
  conversation remains auditable and can still produce the final decision or
  work update.
- Given a connector send fails, when the runtime records the failure, then the
  dashboard shows the failed delivery and next action.
- Given the same Teams message is received twice, when the connector ingests it,
  then idempotency prevents duplicate queue items, work items, or replies.

## Success Signals

- Sponsor direct-message questions receive useful answers without creating work
  noise.
- Separate role identities make the experience feel like working with a
  specialist team.
- Channel traffic falls compared with v1 because only relevant roles respond.
- Queue items created from Teams carry clear source links and role rationale.
- Important decisions are found in the document library rather than only in
  Teams history.
- Sponsor questions and approvals are answerable from Teams threads.
- Agent questions reach the right humans with enough context to answer without
  hunting through raw artifacts.
- Connector failures are visible in the dashboard and recoverable.
- The same conversation model can be reused by Slack and other connectors.

## Product Risks

- Separate role bot identities may improve human feel but increase Teams app
  registration, installation, permission, and support complexity.
- A single gateway bot may simplify operations but feel less like talking to a
  specialist unless role addressing and display are excellent.
- Automatic relevance selection may silence a role that should have spoken.
- Capturing all channel messages as context may create privacy, retention, and
  noise concerns.
- Teams API behavior around bot mentions and message identity may limit how
  closely role agents can appear as independent humans.

## Product Recommendation

Use one connector runtime with separate role-agent identities.

For the user experience, each configured role should appear as its own
addressable specialist in Teams. Implementation may use one connector service
behind those identities, but the runtime must not depend on Teams bot-to-bot
mentions or Teams itself as the orchestration primitive. The runtime owns the
routing decision and stores conversation state.

For the MVP, prioritize:

1. Direct messages to configured role agents.
2. Project-channel role mentions.
3. Team-wide relevance-checked discussion.
4. Agent-initiated human questions by DM, channel, group chat, or thread.
5. Sponsor questions and approval replies bound to the originating thread.
6. Complete Markdown replies and delivery status.

## Sponsor Review Questions And Disposition

The original sponsor questions have been answered and incorporated into this
document. The remaining product question is not a blocker:

- What specific retention durations should the default project templates use
  for raw Teams messages, compacted summaries, and source-linked decisions?

Product Manager recommendation: do not make raw Teams retention the product
source of truth. Preserve raw Teams messages for operational traceability for a
configurable short-to-medium period, compact useful context into summaries, and
record durable decisions, requirements, risks, architecture choices, and release
facts in the document library.

## Downstream Readiness

This product definition is ready for downstream UX and Architecture shaping.
Retention defaults may be finalized during architecture/security review.

Likely downstream roles:

- UX Designer for direct-message, channel, thread, approval, and dashboard user
  experience.
- Solution Architect for connector abstraction, identity model, and conversation
  routing design.
- Security Architect for tenant permissions, privacy, retention, and authority.
- Engineering for v2 connector services, database records, safe-output routing,
  idempotency, and tests.
- QA Engineer for Teams conversation scenarios, duplicate delivery, and
  regression coverage.

## Review Log

- RL-001 | product-manager | sponsor-review | full document | The product
  definition needs sponsor answers on identity model, privacy, channel capture,
  relevance selection, default channel naming, proactive work proposal, and
  retention before downstream handoff. | answered 2026-06-12; identity,
  privacy, channel capture, relevance, project channel, proactive work proposal,
  and document-library truth decisions incorporated. Retention duration remains
  an architecture/security defaulting question.
- RL-002 | product-manager | sponsor-review | agent-initiated human questions |
  Sponsor clarified that agents must be able to message humans for
  clarification or decisions, preferring DM for focused questions and suitable
  channels/group chats with mentions for broader discussion. The system should
  guide rather than rigidly enforce this communication style. | incorporated
  2026-06-12
