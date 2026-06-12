# V2 Teams Connector Test Plan

Status: downstream QA draft

Owner role: QA Engineer

Date: 2026-06-12

Source documents:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`
- `docs/ux/v2-teams-connector-interaction-design.md`
- `docs/architecture/v2-teams-connector-architecture.md`
- `docs/security/v2-teams-connector-security.md`
- `docs/prompt-engineering/v2-teams-connector-behaviour-guidance.md`
- `docs/engineering/v2-teams-connector-implementation-plan.md`

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

### Approval And Human-Response Cards

Scenario: Approval card captures a privileged decision

Given a role sends an approval or human-response request through safe-output
And the request is bound to a work item, approval gate, risk, or release
  decision
When an authorized sponsor or approver submits the Teams response
Then the response is normalized and stored against the originating runtime
  object
And the Teams card or structured message updates visibly where supported
And the audit record includes actor, authority, source Teams reference,
  correlation id, decision, timestamp, and resulting artifact link.

Scenario: Unauthorized approval response fails closed

Given an untrusted or unauthorized Teams user submits an approval, release, or
  risk-acceptance response
When the connector processes the response
Then the privileged runtime state is not changed
And the rejected response is recorded with the authorization reason
And an operator-visible attention item or human clarification is created
  according to policy.

Scenario: Stale or failed card submission is recoverable

Given an approval card is stale, superseded, or cannot be updated in Teams
When a human submits or views the card
Then the connector does not apply an ambiguous decision
And the runtime records a delivery or submission attention item with the next
  action
And the human receives a clear status or recovery link where policy allows.

### Dashboard And Observability

Scenario: Status surfaces show connector state without leaking private text

Given the connector has active conversations, role identities, channels,
  deliveries, relevance checks, context summaries, promotions, permission
  checks, and attention items
When status JSON and HTML views are rendered
Then connector health and counts are visible
And private direct-message body text is redacted unless an
  authorization-aware view explicitly permits it
And each attention item includes owner, reason, retryability, and next action.

Scenario: Telemetry records connector flow with redacted identifiers

Given inbound Teams events, routing, safe-output delivery, retries, response
  binding, compaction, and permission validation occur
When telemetry is emitted
Then required spans, metrics, and logs include project id, connector id,
  conversation id, role id where applicable, correlation id, and redacted
  external message ids
And credentials, tokens, private message bodies, and restricted content are not
  emitted.

### Release And Rollback

Scenario: Project-scoped deployment runs without mixing system and project
  state

Given a project-scoped connector deployment profile is configured
When the connector service starts with the v2 runtime and configured database
Then Compose or deployment validation succeeds
And source repo artifacts, project deployment outputs, secrets, and local
  runtime state remain in their configured boundaries.

Scenario: Connector rollback preserves audit state

Given the Teams connector is enabled and has conversation, delivery, attention,
  and audit records
When rollback or emergency disablement is executed
Then connector ingress and outbound delivery are disabled according to the
  documented procedure
And runtime state, conversation records, delivery records, idempotency
  receipts, and audit events are preserved for review.

## Feature Story Coverage And Release Gates

QA will track the Engineering feature-story breakdown as the release spine.
Story 1 may ship only as an internal foundation slice. The Teams connector is
not release-complete until Stories 1 through 14 pass or a sponsor-approved
scope reduction records residual risks, owners, and follow-up story numbers.

| Engineering story | BDD and regression coverage | Required evidence | Story release gate |
| --- | --- | --- | --- |
| Story 1 - Connector Foundation And Local Test Adapter | Repository, migration, config, fixture replay, duplicate receipt, thread reply, send-failure fixture, and status JSON foundation tests. | Schema migration output, connector config fixture, replay fixture logs for DM/channel/mention/duplicate/thread/send failure, event log samples, status JSON table/count sample. | Internal foundation only; no real Teams tenant dependency; all connector-neutral tables, repositories, fixtures, and health/status counts pass automated tests. |
| Story 2 - Role Direct Messages And `status.reply` | DM conversational reply, DM promotion negative baseline, no-work-by-default, duplicate DM idempotency, Markdown completeness, private preview redaction. | Automated tests proving one delivery record and one reply, unchanged queue/work tables for ordinary DMs, duplicate event suppression, redacted status output. | Release only if ordinary DMs are private, reply through `status.reply`, create no durable work by inference, and cannot leak private body text in default status views. |
| Story 3 - Project Channel Capture And Role Mentions | Unmentioned channel context capture, focused role mention, multiple mentions, unknown mention failure, no low-value acknowledgements, thread binding. | Fixture replay evidence, role assignment records, no-wake/no-reply evidence for unmentioned context, attention item for unknown mention, event log route records. | Release only if channel messages never wake all roles by default and role routing depends on configured identities or aliases, not guesses. |
| Story 4 - Agent-Initiated Human Questions And Thread Binding | `sponsor.ask_question` by DM/channel/group/thread, sponsor answer binding, human loop-in, ambiguous reply attention, no mutation on ambiguous binding. | Safe-output request and delivery records, thread binding records, normalized human response event, loop-in participant audit, ambiguous-binding attention item. | Release only if every outbound human question has a source runtime binding and replies continue the correct flow without free-text state mutation. |
| Story 5 - Delivery, Retry, Failure Attention, And Idempotency | Delivery state transitions, transient retry, permanent failure, unknown outcome, duplicate safe-output delivery, failed-delivery fake-claim prevention. | Delivery record transition history, retry policy evidence, external message id or failure metadata, connector attention item, event log send attempts. | Release only if failed or unknown delivery is visible and no role/status reply can claim a human received a Teams message unless delivery status supports it. |
| Story 6 - Separate Visible Role Identities | Config validation for identity model, role display names, aliases, mention handles, external app/bot ids, per-role disablement, authorization not based on display name. | Emulator identity fixtures, selected identity-model record, real tenant smoke evidence when consent is approved, limitations and emergency disablement evidence. | Release only after Security approves the identity model and QA proves runtime authorization uses configured bindings rather than Teams display names. |
| Story 7 - Feature, Epic, Incident, And Focused-Work Channels | Focus channel binding, scoped context capture, focus-channel role mentions, thread behavior parity, private-channel explicit binding and permission validation. | Config fixture with focus channel, scoped conversation records, dashboard/status source channel scope, private-channel permission check evidence. | Release only if focus channels preserve project identity, narrow context correctly, and private channel capture fails closed without explicit binding and permission. |
| Story 8 - Team-Wide Relevance Checks | Team-wide trigger, per-role relevance/no-op records, non-relevant silence, below-threshold justified response, no Teams bot-to-bot consult. | `team_wide_prompt` record, relevance scores/thresholds/reasons, no-op records, selected deliveries, consult/handoff runtime records where applicable. | Release only if channel-noise regressions pass and every configured role has auditable relevance disposition without forcing every role to post. |
| Story 9 - Proactive Work Proposals And Conversation Promotion | Value-rationale proposal, low-value no-proposal, DM promotion privacy, source refs/classification/redaction, fake work-item claim prevention. | Safe-output proposal record, source Teams refs, promotion record, queue/work artifact link when created, negative evidence for free-text messages. | Release only if durable work or promotion happens solely through safe-output/runtime APIs and Teams replies report only artifacts that actually exist. |
| Story 10 - Approval And Human-Response Cards | Approval card delivery, authorized submission, unauthorized submission, stale/superseded card, submission failure, visible card update where supported. | Card/message payload fixture, normalized response record, authority check evidence, audit event, failure attention item, Teams client limitation note. | Release only if privileged responses enforce configured authority and ambiguous, stale, or failed submissions do not mutate approval, risk, or release state. |
| Story 11 - Context Compaction, Retention, And Durable Knowledge | Retention policy config, raw history expiry, source-linked compaction, private compaction privacy, durable decision survival, audit metadata/hash retention. | Retention config sample, compaction output with source refs, durable artifact or memory link, deletion/expiry evidence, privacy redaction evidence. | Release only if important decisions, requirements, risks, approvals, blockers, and release facts survive raw history expiry without laundering private DMs into shared context. |
| Story 12 - Permission, Consent, Installation, And Authority Hardening | Startup validation, missing/revoked consent, unauthorized sponsor/operator, broad Graph permission approval, credential redaction, outside-boundary event rejection. | Permission check records, tenant consent package, app installation evidence, authority mapping fixture, redacted logs/traces/status, failure attention item. | Release only if missing or revoked permissions fail closed and real tenant release has documented consent, permission, owner, rotation, and disablement evidence. |
| Story 13 - Dashboard And Observability Completion | Status JSON contract, HTML smoke, connector attention visibility, private body redaction, OpenTelemetry spans, metrics, redacted logs. | Status JSON/HTML samples, telemetry trace and metric samples, redaction proof, attention item examples for delivery, permission, compaction, and ambiguity. | Release only if operators can inspect health, failures, duplicates, relevance, promotions, compaction, and permissions without exposing private or restricted content. |
| Story 14 - Release, Deployment, Rollback, And Dogfood Validation | Project-scoped Compose/deployment validation, real Teams smoke, inbound/outbound scenario set, failure smoke, rollback/disablement, release evidence linking. | Compose or deployment validation output, real Teams tenant smoke package when approved, rollback record, known residual risks, release record links. | Complete connector release only if the full smoke set passes, rollback preserves audit state, and release evidence links product, UX, architecture, security, prompt, engineering, QA, and release artifacts. |

## Coverage Gaps And Engineering Review Requests

- CG-001 | Story 1 | Testability gap | Engineering should specify exact
  table names, migration versioning, repository contracts, and status JSON
  fields before QA can write stable contract tests. If table names remain
  provisional, QA will gate only on documented logical records and fixture
  behavior.
- CG-002 | Stories 4, 9, and 10 | Safe-output schema gap | The implementation
  plan names required payload fields but leaves exact schemas open. Engineering
  should provide versioned payload contracts for `status.reply`,
  `sponsor.ask_question`, queue/work proposal, approvals, human responses,
  context promotion, decision/risk/document updates, and release/status
  deliveries before story release.
- CG-003 | Stories 5 and 14 | Retry policy gap | QA cannot assert unknown
  outcome behavior without a concrete retry policy, operator resolution path,
  retry limits, and resend/supersede rules. Engineering should document these
  before delivery/retry release.
- CG-004 | Stories 6 and 12 | Identity and permission dependency | Separate
  visible role identity release is blocked until Security and Engineering
  select the first Teams substrate, identity model, permission set, consent
  path, app owner model, credential rotation path, and emergency disablement
  procedure.
- CG-005 | Story 7 | Focus channel scope gap | Engineering should define how
  focus channel bindings identify feature, epic, incident, or work scope, and
  how private Teams channel membership and permission validation are surfaced
  in status.
- CG-006 | Story 8 | Relevance evaluator contract gap | QA needs a deterministic
  fixture contract for relevance score, threshold, decision, reason, no-op, and
  exception fields. Prompt-only guidance is not sufficient for regression
  gating.
- CG-007 | Story 10 | Adaptive Card feasibility gap | Engineering should record
  which Teams clients and tenant policies support card update behavior. If card
  updates are not uniformly supported, the story needs an approved structured
  message fallback with equivalent audit evidence.
- CG-008 | Story 11 | Retention deletion mechanism gap | Security recommends
  deletion or cryptographic shredding after retention expiry, but Engineering
  has not selected the storage-adapter mechanism. QA will require manual
  evidence for the chosen backend before release.
- CG-009 | Story 13 | Dashboard authorization gap | Default redaction is
  required until authorization-aware status views exist. Engineering should
  define whether Story 13 includes authorization-aware drilldowns or keeps all
  private bodies redacted.
- CG-010 | Story 14 | Real tenant smoke dependency | Emulator evidence is
  enough for early slices, but the complete connector release requires real
  tenant smoke evidence after consent is approved. Deferring real tenant smoke
  must be a sponsor-approved release risk, not a QA pass.

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

Before a story release, capture the evidence listed in the story matrix above.
Before the complete connector release, capture evidence for:

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
- Approval and human-response card or structured-message evidence, including
  unauthorized, stale, and failed submission behavior.
- Dashboard, status JSON, HTML smoke, OpenTelemetry, metrics, and redaction
  evidence.
- Project-scoped deployment validation, real Teams tenant smoke evidence when
  permissions are approved, rollback or disablement evidence, and known
  residual risks.

## Exit Criteria

- All BDD scenarios and story-level regression gates pass in automated or
  documented manual form for the stories included in the release.
- All idempotency, delivery failure, permission failure, privacy, and retention
  tests have recorded evidence.
- Story 1 is treated as foundation-only and is not marketed or declared as a
  complete Teams connector.
- The complete Teams connector is not declared release-ready until Stories 1
  through 14 are complete or explicit sponsor scope reduction records residual
  risks, owners, mitigations, and target follow-up stories.
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
- RL-002 | qa-engineer | engineering-story-alignment | feature story coverage
  and release gates | Expanded QA coverage to track the full Engineering
  feature-story breakdown rather than the initial MVP surface. Added
  story-to-BDD/regression evidence mapping, story release gates, approval/card,
  dashboard/observability, release/rollback scenarios, coverage gaps, and
  Engineering review requests for testability blockers. | incorporated
  2026-06-12
