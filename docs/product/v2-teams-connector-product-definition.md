# V2 Teams Connector Product Definition

Status: sponsor review

Owner role: product-manager

Date: 2026-06-12

Source: sponsor request to design the new Teams connector for the v2 runtime.

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
- Sponsor questions and approvals are visible, actionable, and routed back to
  the correct conversation.
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

### Project Development Channel

Each project may configure one or more Teams channels as project rooms. A
message in a project room is project context by default.

Unmentioned channel messages should be stored as shared context and made
available to future role runs according to project retention and visibility
rules. They should not automatically wake every role.

### Mentioned Role In Channel

When a human mentions a configured role in the project channel, the connector
should route the message to that role as a focused conversation assignment.

Other roles should not respond unless explicitly consulted, handed off to, or
included by a relevant team-wide trigger. The original channel message remains
available as shared context.

### Team-Wide Discussion

When a human addresses the configured team-wide trigger, the runtime should run a
relevance step before role replies are posted.

Relevant roles may answer, ask clarifying questions, propose work, consult
another role, or no-op. Non-relevant roles should stay quiet, while the runtime
records enough audit data to explain that relevance was considered.

The product experience should be closer to asking a meeting room "does anyone
have anything to add?" than instructing every person to produce a report.

### Thread Binding

Replies in a Teams thread should remain bound to the original conversation,
queue item, work item, sponsor question, or approval request where applicable.

Thread replies must be processed as part of the same conversational context
unless the human explicitly starts new work.

## Scope

V2 Teams connector MVP scope:

- Teams inbound direct messages to configured role identities.
- Teams inbound project-channel messages.
- Teams inbound role mentions and configured team-wide triggers.
- Conversation records in the v2 database.
- Role reply routing through `status.reply`.
- Sponsor question routing from `sponsor.ask_question`.
- Queue/work proposal from conversation through safe-output tools.
- Teams delivery records and visible delivery failures.
- Markdown rendering for full agent replies.
- Project-channel context capture.
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
- Map Teams users to runtime people, sponsor candidates, and operator authority
  where configured.
- Map Teams role identities or aliases to v2 role services.
- Store inbound Teams messages as conversation events with source metadata.
- Route direct messages to the addressed role without creating a work item by
  default.
- Route mentioned channel messages to the mentioned role while preserving shared
  context.
- Run team-wide relevance selection before waking roles for an all-team request.
- Send complete Markdown replies through Teams with no artificial truncation.
- Suppress low-value acknowledgement and "started" messages for ordinary direct
  conversations.
- Send explicit progress messages only when work is long-running, blocked,
  waiting for human input, or crossing a lifecycle gate.
- Bind sponsor replies to the original question, approval, or work item.
- Record connector delivery status, failures, retries, and final message ids.
- Ensure every durable state change comes from safe-output calls, not inferred
  connector text.

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
- Given a sponsor replies in a Teams thread to a question or approval, when the
  connector processes the reply, then the runtime binds it to the original
  conversation/work item and continues the correct flow.
- Given a connector send fails, when the runtime records the failure, then the
  dashboard shows the failed delivery and next action.
- Given the same Teams message is received twice, when the connector ingests it,
  then idempotency prevents duplicate queue items, work items, or replies.

## Success Signals

- Sponsor direct-message questions receive useful answers without creating work
  noise.
- Channel traffic falls compared with v1 because only relevant roles respond.
- Queue items created from Teams carry clear source links and role rationale.
- Sponsor questions and approvals are answerable from Teams threads.
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

Use one connector runtime with role-addressable conversation endpoints.

For the user experience, support role-specific identities or aliases where the
tenant allows it, but do not make the runtime depend on Teams bot-to-bot
mentions or one bot per role as the orchestration primitive. The runtime should
own the routing decision and store all conversation state.

For the MVP, prioritize:

1. Direct messages to configured role agents.
2. Project-channel role mentions.
3. Team-wide relevance-filtered discussion.
4. Sponsor questions and approval replies bound to the originating thread.
5. Complete Markdown replies and delivery status.

## Sponsor Review Questions

These questions affect scope and should be answered before this is handed to
Architecture and Engineering:

1. Should the v2 MVP present role agents as separate Teams identities
   (`AM-Product Manager`, `AM-Release Manager`), or is a single `Agentic Mesh`
   bot acceptable if it can route to role personas cleanly?
2. Should direct messages to a role be private to that human and role unless
   explicitly promoted into work, or should all role DMs become project context
   by default?
3. Should every project-channel message be retained as shared context, or only
   messages in configured channels/threads that match capture rules?
4. For a team-wide request, should a cheap relevance classifier decide which
   roles wake, or should every role service receive a lightweight relevance task
   and decide whether to reply?
5. What should the default project channel be called in examples: `all-agents`,
   `development`, or another name?
6. Should role agents proactively propose work from a channel discussion when
   they infer durable work is needed, or only when the human explicitly asks?
7. What retention period should apply to Teams conversation context before it is
   summarized or expired?

## Downstream Readiness

The slice is not ready for implementation until the sponsor answers the review
questions or explicitly accepts Product Manager defaults.

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
  retention before downstream handoff. | open
