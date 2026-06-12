# V2 Teams Connector Test Plan

Status: downstream QA draft

Owner role: QA Engineer

Date: 2026-06-12

Source documents:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`

## Purpose

This test plan defines BDD acceptance, regression, and failure-mode coverage for
the v2 Teams connector.

The plan verifies that Teams behaves as a human collaboration surface while the
v2 runtime remains the authority for routing, state, safe-output calls,
approvals, delivery records, audit, and durable work.

## Quality Goals

- Direct messages to role agents remain conversational by default.
- Durable work is created only through explicit safe-output intent.
- Project channel messages are captured as shared context without waking every
  role.
- Role mentions route focused work to the addressed role only.
- Team-wide prompts produce relevance-checked specialist responses, not noisy
  all-role replies.
- Agents can ask humans for clarification, decisions, approvals, or risk
  acceptance through appropriate Teams routes.
- Humans can loop in other humans or agents without losing auditability.
- Delivery, idempotency, permission, privacy, retention, and release evidence
  are observable and testable.

## Assumptions

- The connector stores inbound Teams events as v2 conversation records with
  Teams conversation references, message references, visibility scope,
  participant identities, and correlation ids.
- Role replies, sponsor questions, proactive work proposals, and durable state
  changes flow through safe-output handlers.
- Teams is not used for role-to-role consults, handoffs, lifecycle transitions,
  or agent orchestration.
- Retention durations are configurable and may be finalized by architecture and
  security review after this plan is drafted.

## Test Data And Fixtures

- Project: `agentic-mesh-dev`
- Project Team: configured Teams team with default `project` channel.
- Focus channel: configured feature or epic channel.
- Roles: Product Manager, Engineering, Solution Architect, Security Architect,
  QA Engineer, Release Manager.
- Humans: sponsor, delivery owner, security reviewer, untrusted tenant member.
- Team-wide trigger: configured all-role request phrase or mention.
- Work source: existing queue item, approval, risk, or document update for
  thread-binding and agent-initiated question tests.

## BDD Scenarios

### Direct Messages

Scenario: Role answers a direct message without creating work

Given a sponsor sends a direct message to the Product Manager role
And the message asks a conversational product question
When the connector ingests the Teams message
Then a private Teams conversation record is created or continued
And the Product Manager receives the recent DM context and project context
And Teams receives exactly one complete Markdown reply
And no queue item or work item is created
And no unrelated role is assigned to respond.

Scenario: Direct message can be promoted into tracked work

Given a sponsor sends a direct message to the Product Manager role
And the message asks for a feature to be built
When the role determines the request should become durable work
Then the role creates or proposes the queue item through a safe-output call
And the Teams reply explains the rationale and includes the work link
And the resulting work item records the Teams source reference
And the original DM remains private except for the explicitly promoted work
  context.

Scenario: Ordinary direct message suppresses low-value progress noise

Given a sponsor sends a simple direct question to a role
When the role can answer in one normal turn
Then the connector does not send separate "received", "started", or
  placeholder progress messages
And the final role reply is complete.

### No Work By Default

Scenario: Conversational text does not create durable work by inference

Given an inbound Teams message contains discussion, preference, or background
context
And no role or human explicitly asks to create tracked work
When the message is processed
Then the runtime stores the conversation event
And no queue item, work item, approval, risk, document update, or memory update
  is created from free text alone.

Scenario: Durable state change requires safe-output evidence

Given a Teams conversation includes words that look like a task
When no safe-output call is emitted by the role
Then the runtime audit trail shows no durable work was created
And the connector does not synthesize a work item from message text.

### Project Channel Context Capture

Scenario: Unmentioned project channel message is captured as shared context

Given a sponsor posts an unmentioned note in the configured `project` channel
When the connector ingests the message
Then the message is stored as shared project conversation context
And no role is automatically woken or assigned
And the note can be discovered by later role work according to retention and
  visibility rules.

Scenario: Focus channel narrows context without changing project identity

Given a configured feature channel belongs to the same Project Team
When a human posts an unmentioned message in that channel
Then the message is stored with the same project identity
And the source channel is preserved
And later context retrieval can prefer that channel for related feature work.

Scenario: Important channel decision survives compaction

Given a project channel thread contains a product decision with source
  references
When raw channel history is compacted or expires
Then the decision remains preserved in the document library or source-linked
  memory
And the preserved summary cites the original Teams thread or message.

### Role Mentions

Scenario: Mentioned role receives focused channel assignment

Given a sponsor posts in the project channel and mentions the Engineering role
When the connector processes the mention
Then Engineering receives a focused conversation assignment
And the original message is stored as shared project context
And no other role replies unless consulted, handed off to, or included by a
  separate team-wide trigger.

Scenario: Multiple mentioned roles each receive explicit assignments

Given a sponsor mentions Product Manager and Security Architect in one channel
  message
When the connector resolves both role identities
Then each mentioned role receives the same source message with its role-specific
  assignment
And replies remain bound to the same Teams thread
And the audit record explains why each role was routed.

Scenario: Unknown role mention fails visibly without misrouting

Given a channel message mentions an alias that is not configured for the
  project
When the connector processes the message
Then no role is assigned by guesswork
And the connector records a routing failure or operator-visible warning
And the human receives a concise clarification when policy allows.

### Team-Wide Relevance Checks

Scenario: Team-wide trigger runs lightweight relevance checks

Given a sponsor posts a configured team-wide request in the project channel
When the connector ingests the message
Then every configured role performs a lightweight relevance check
And relevance decisions, scores, thresholds, and reasons are recorded
And only roles with material specialist input respond.

Scenario: Non-relevant roles stay quiet

Given a team-wide request is relevant only to Security Architect
When relevance checks complete
Then Security Architect may respond
And non-relevant roles produce no Teams replies
And their no-op relevance decisions remain auditable.

Scenario: Below-threshold role may respond with justification

Given a role scores below the configured relevance threshold
And the role judges it has important specialist input
When the role chooses to respond
Then the response is posted
And the audit record includes the below-threshold justification.

Scenario: Team-wide trigger does not use Teams for role-to-role consults

Given a role needs another role's specialist input after a team-wide request
When it consults or hands off to that role
Then the consult or handoff uses v2 runtime mechanisms
And Teams contains only human-visible summaries or questions as appropriate.

### Agent-Initiated Human Questions

Scenario: Agent asks a focused human question by direct message

Given a role is blocked on a sponsor clarification for an active work item
When the role chooses direct-message routing
Then the sponsor receives a Teams DM with the question, context, requested
  decision, and link to the originating work
And the runtime records intended audience, reason, source work context,
  delivery channel, and Teams message id.

Scenario: Agent asks a broader decision question in a channel

Given a role needs a visible decision from multiple humans
When the role chooses channel or group-chat routing
Then the Teams message mentions the required humans or roles
And the thread remains bound to the originating work item, queue item,
  approval, risk, or document context
And replies continue the correct runtime flow.

Scenario: Sponsor answer binds back to original question

Given a role sent an agent-initiated question in Teams
When the sponsor replies in the same Teams thread
Then the connector binds the reply to the original question and source work
And the role receives the answer as the next input
And the work item or approval state is updated only through safe-output calls.

### Human Loop-In

Scenario: Human loops another human into a direct clarification

Given a role asks a sponsor a focused direct-message question
When the sponsor adds or redirects the discussion to another accountable human
Then the connector records the added participant and route change
And the conversation remains linked to the originating work
And the final decision can still be captured as durable evidence.

Scenario: Human loops another role into a Teams discussion

Given a human adds a role mention to an existing agent-initiated discussion
When the connector processes the new mention
Then the newly mentioned role receives a focused assignment only if project
  policy allows that route
And any role-to-role follow-up still uses runtime consult or handoff tools
And the Teams thread remains human-visible context, not the agent bus.

### Proactive Work Proposal

Scenario: Role proposes work from conversation with value rationale

Given a channel conversation reveals a necessary action
And no human has explicitly created tracked work yet
When a role can explain the value and urgency
Then it may propose a queue item or work item through safe-output tools
And Teams receives a concise rationale and link
And the proposal cites the source conversation.

Scenario: Role does not propose low-value work

Given a conversation contains speculative or low-confidence ideas
When a role cannot justify necessity and value
Then it does not create or propose tracked work
And it may reply conversationally or stay quiet according to context.

## Regression And Failure-Mode Coverage

### Idempotency

- Duplicate inbound Teams event with same tenant, conversation id, message id,
  and role route creates one conversation event only.
- Duplicate direct-message event produces at most one role reply.
- Duplicate channel role mention creates at most one focused assignment.
- Duplicate team-wide trigger does not rerun relevance checks after the first
  successful idempotent processing window unless explicitly reprocessed by an
  operator.
- Duplicate safe-output delivery retry does not create duplicate Teams messages
  unless the previous delivery has no final Teams message id and retry policy
  permits another send.
- Duplicate proactive work proposal source does not create duplicate queue or
  work items.

### Delivery Failures

- Teams send failure records connector, destination, payload class, error,
  retry count, final state, and correlation id.
- Failed direct-message reply is visible in connector status or dashboard
  views with the next operator action.
- Failed channel reply does not mark the role work as successfully delivered.
- Retry after transient Teams failure preserves idempotency and thread binding.
- Permanent delivery failure leaves the underlying runtime state intact and
  records that human notification did not succeed.
- Partial Markdown/card rendering failure falls back to a policy-approved plain
  Markdown message or records a visible delivery failure.

### Permission Failures

- Missing Teams app consent prevents connector startup or marks the binding
  unhealthy with an actionable error.
- Missing channel read permission prevents project-channel capture and is
  visible before release.
- Missing direct-message permission prevents DM routing and records a clear
  delivery or ingestion failure.
- Unauthorized human cannot route work, approve gates, or access private DM
  context beyond configured authority.
- Untrusted tenant member posting in a project channel is handled according to
  project policy and never grants sponsor or operator authority by implication.
- Revoked permissions during operation fail closed and do not create inferred
  work from incomplete context.

### Privacy

- Direct-message context is scoped to the human and addressed role until
  explicitly promoted.
- Promoted work stores only the necessary source-linked context allowed by
  policy.
- Private DM content is not included in unrelated project-channel summaries.
- Project-channel context is shared only within the configured project
  visibility boundary.
- Feature or epic channel context respects channel membership and project
  confidentiality settings.
- Audit views expose enough metadata for traceability without leaking private
  message body content to unauthorized users.

### Retention And Compaction

- Raw Teams messages expire or compact according to configured retention
  policy.
- Compacted summaries preserve important decisions, requirements, constraints,
  risks, instructions, approvals, and blockers with provenance.
- Durable decisions are written to the document library or source-linked memory
  before raw history is removed.
- Role memory entries cite Teams conversations, documents, work items, or events
  and remain concise.
- Compaction does not merge private DM content into shared project summaries
  unless explicitly promoted.
- Retention policy changes affect future processing predictably and do not
  silently delete required release evidence.

### Teams Formatting And Thread Binding

- Complete Markdown replies render without artificial truncation.
- Long replies are delivered as complete messages or approved split messages
  with stable ordering.
- Thread replies bind to the original conversation, work item, sponsor question,
  or approval request.
- Replies to old or archived threads are either processed against the existing
  binding or produce an operator-visible routing failure.
- Status links use the configured external base URL.

### Relevance And Noise Regressions

- Unmentioned channel messages never wake every role.
- Mentioning one role does not make all roles reply.
- Team-wide relevance checks do not produce default acknowledgements from every
  role.
- Low-confidence relevance does not silently suppress required specialist input
  when a role records a justified exception.
- Prompt regressions that claim fake work item ids, fake approvals, or fake
  deployments are rejected by safe-output validation.

## Release Evidence Requirements

Before release, capture evidence for:

- Connector configuration for Project Team, `project` channel, optional focus
  channel, role aliases, team-wide trigger, allowed users or groups, and
  external base URL.
- Teams and Entra permission validation, including least-privilege consent
  results and known limitations.
- Direct-message smoke test showing one complete reply and no default work item.
- Direct-message promotion test showing safe-output-created work and source
  link.
- Unmentioned project-channel context capture test showing no role wake-up.
- Role mention test showing focused assignment and bounded reply behavior.
- Team-wide trigger test showing relevance records and only relevant replies.
- Agent-initiated human question test showing delivery, thread binding, and
  answer processing.
- Human loop-in test showing participant/routing audit and final decision
  capture.
- Proactive work proposal test showing value rationale and safe-output source.
- Idempotency test using replayed inbound Teams events.
- Delivery failure test showing visible connector failure and retry behavior.
- Permission failure test showing fail-closed behavior and actionable status.
- Privacy test proving private DM content is not leaked into shared context.
- Retention/compaction test proving important decisions survive raw history
  expiry with provenance.
- Regression test results for runtime safe-output validation and connector
  status read models.

## Exit Criteria

- All MVP BDD scenarios pass in automated or documented manual form.
- All idempotency, delivery failure, permission failure, privacy, and retention
  tests have recorded evidence.
- Any unresolved defect has a severity, owner, release decision, and rollback or
  mitigation note.
- Release Manager has a smoke-test checklist and rollback or connector-disable
  procedure.
- Product, architecture, security, engineering, QA, and release evidence links
  are collected in the release record.

## Review Log

- RL-001 | qa-engineer | downstream-handoff | full document | Created the QA
  test plan from the v2 Teams connector product definition and downstream
  handoff. Coverage includes BDD scenarios, regression and failure modes,
  privacy, retention, idempotency, delivery and permission failures, and release
  evidence. | incorporated 2026-06-12
