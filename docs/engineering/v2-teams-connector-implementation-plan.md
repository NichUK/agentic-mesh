# V2 Teams Connector Implementation Plan

Status: engineering implementation plan

Owner role: engineering

Date: 2026-06-12

Source documents:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`
- `docs/ux/v2-teams-connector-interaction-design.md`
- `docs/architecture/v2-teams-connector-architecture.md`
- `docs/security/v2-teams-connector-security.md`
- `docs/prompt-engineering/v2-teams-connector-behaviour-guidance.md`
- `docs/qa/v2-teams-connector-test-plan.md`
- `docs/architecture/v2-runtime-reset.md`

## Purpose

This plan defines implementation of the full v2 Microsoft Teams connector
product and design plan, delivered as ordered feature stories that can be
implemented, tested, reviewed, and released one slice at a time.

The first slice should establish the connector foundation, but the target is
complete: separate role identities, direct messages, project channels,
feature and epic channels, team-wide relevance checks, agent-initiated human
questions, proactive work proposals, context capture and compaction,
safe-output integration, dashboard visibility, permission validation,
delivery/idempotency, and release/deployment readiness.

Teams is a human collaboration connector. It is not agent-to-agent transport.
Role-to-role consults, handoffs, blockers, lifecycle transitions, relevance
decisions, work promotion, approvals, release decisions, and closure must use
v2 runtime queues, database state, event log records, and safe-output calls.
Teams may show human-visible replies, questions, summaries, approvals, status
links, and project-room context.

## Complete Target Scope

The complete connector should provide:

- project-scoped Teams connector configuration with tenant, team, channel,
  role identity, human authority, retention, and external URL bindings
- separate visible role identities in Teams, implemented through separate
  app/bot registrations, one role-addressable gateway app, or a hybrid
  deployment model
- role direct messages that are private by default and conversational until
  explicitly promoted
- project-channel capture as shared project context without waking every role
- feature, epic, incident, or focused-work channels that narrow context while
  preserving the project identity
- role mention routing that assigns only addressed roles unless runtime consult
  or handoff includes another role
- team-wide trigger handling with lightweight relevance checks, thresholds,
  no-op records, and justified below-threshold responses
- agent-initiated human questions by DM, thread, channel, or group chat with
  bindings back to work, approval, risk, or document context
- proactive work proposal from conversation through safe-output tools with
  source links, rationale, and no free-text state mutation
- sponsor questions, approvals, human responses, and risk acceptance bound to
  the originating conversation, thread, work item, approval, or gate
- Teams-compatible Markdown replies and Adaptive Card extension points for
  structured human response contracts
- conversation context capture, retention, redaction, source-linked compaction,
  and durable preservation of decisions, risks, requirements, and release facts
- inbound idempotency, outbound idempotency, delivery records, retries,
  unknown-outcome handling, and connector attention items
- status/dashboard visibility for connector health, active conversations,
  delivery failures, relevance decisions, compaction, promotions, and
  unresolved attention
- permission, consent, installation, team binding, channel binding, and
  authority validation that fails closed
- OpenTelemetry spans, metrics, and redacted logs for connector receive,
  routing, safe-output delivery, retry, compaction, and release operations
- deployment and rollback procedures for local development, dogfood, and real
  Teams tenant profiles

## Module And Service Boundaries

Use the existing v2 package as the integration point. Keep connector concepts
connector-neutral where practical so future Slack, issue tracker, email, web,
or CLI connectors can reuse the runtime model.

Proposed modules:

- `agentic_mesh_v2.connectors.models`: connector-neutral dataclasses for
  participants, conversations, conversation events, delivery requests,
  delivery results, idempotency receipts, relevance checks, context summaries,
  promotions, and route decisions.
- `agentic_mesh_v2.connectors.repository`: database repository methods for
  connector records. It should wrap `V2Database` rather than spreading raw SQL
  across the adapter.
- `agentic_mesh_v2.connectors.routing`: route direct messages,
  project-channel context, feature-channel context, role mentions,
  team-wide prompts, thread replies, human question responses, approvals, and
  ambiguous events into runtime actions.
- `agentic_mesh_v2.connectors.delivery`: consume safe-output delivery requests
  and create/update delivery records.
- `agentic_mesh_v2.connectors.relevance`: coordinate team-wide relevance work,
  store role decisions, and select human-visible replies.
- `agentic_mesh_v2.connectors.context`: capture, retain, redact, compact, and
  promote conversation context into source-linked summaries or durable project
  artifacts.
- `agentic_mesh_v2.connectors.permissions`: validate connector installation,
  consent, team/channel binding, participant authority, and role identity
  configuration.
- `agentic_mesh_v2.connectors.teams.adapter`: Teams-specific mapping between
  Bot Framework or Graph payloads and connector-neutral inbound/outbound
  models.
- `agentic_mesh_v2.connectors.teams.service`: connector service entry point
  for receive, route, send, retry, compaction scheduling, health checks, and
  setup validation.
- `agentic_mesh_v2.connectors.teams.config`: project binding loader and
  validation for connector id, tenant id, team/channel bindings, role aliases,
  allowed users/groups, retention settings, Teams identity model, and external
  base URL.
- `agentic_mesh_v2.server`: extend the current status snapshot and HTML view
  with connector health, conversations, relevance checks, compaction,
  promotions, delivery failures, and attention items.

Do not put Teams-specific concepts in `role_service.py` or
`safe_outputs.py`. Role services should receive normalized assignments and
emit safe-output calls without knowing whether the source connector is Teams.

## Database And Schema Changes

The current v2 schema has a minimal `conversations` table. The connector should
add explicit tables rather than overloading that table with Teams-specific
text fields. SQLite remains the first backend, but repositories should use
portable SQL patterns that can move to Postgres later.

Required logical tables:

- `connectors`: configured connector instances with `connector_id`,
  `project_id`, `connector_type`, `display_name`, `status`, `config_version`,
  permission state, installation state, and timestamps.
- `connector_role_identities`: role aliases and Teams-facing bindings with
  `connector_identity_id`, `connector_id`, `role_id`, optional
  `role_instance_id`, display name, mention handle, external app or bot id,
  identity model, active flag, and timestamps.
- `connector_channels`: project, feature, epic, incident, approval, and role
  channel bindings with connector id, source type, external team id, external
  channel id, visibility scope, topic or work scope, and active flag.
- `participants`: normalized humans, sponsors, operators, roles, role
  instances, and external services.
- `participant_authorities`: authority labels such as sponsor, operator,
  approver, release approver, risk accepter, and project member.
- `conversations_v2` or an additive migration of `conversations`: conversation
  type, project id, connector id, visibility scope, external tenant/team/chat/
  channel/thread ids, root external message id, bound queue/work/safe-output/
  approval/document/risk refs, status, classification, and timestamps.
- `conversation_participants`: participants in each conversation and their
  relation to the conversation.
- `conversation_events`: normalized inbound and outbound events, sender,
  mentions, body text or Markdown, attachment metadata, card metadata, event
  kind, source permalink, retention class, classification, redaction state,
  correlation id, and timestamps.
- `thread_bindings`: external thread or message ids bound to runtime queue
  items, work items, safe-output calls, approvals, questions, risks, release
  gates, or document refs.
- `external_event_receipts`: inbound idempotency receipts keyed by connector,
  tenant, external conversation/channel/chat, message id, event type, and
  version marker.
- `delivery_records`: outbound delivery requests and attempts with destination,
  source safe-output call or runtime notification, purpose, status, retry
  count, next retry time, external message id, error class, and timestamps.
- `relevance_checks`: team-wide prompt checks by role with score, threshold,
  decision, reason, no-op status, exception reason, and resulting safe-output
  or delivery refs.
- `conversation_context_summaries`: compacted, source-linked summaries with
  source message refs, project or feature scope, summary type, target artifact
  or memory ref, classification, and retention state.
- `conversation_promotions`: explicit promotions from private or conversational
  context into queue items, work items, role memory, document updates, risks,
  decisions, or release evidence.
- `connector_permission_checks`: setup and runtime permission validation
  results with consent type, app/install state, checked capability, status,
  actor, and timestamp.
- `connector_attention_items`: unresolved setup, permission, delivery,
  idempotency, compaction, privacy, and ambiguous-binding states with owner,
  reason, next action, retryability, and status.

Optional implementation detail:

- raw connector payloads may be stored in a table or external blob reference
  only for the configured retention period. Normal runtime behavior must use
  normalized records, not raw Teams JSON.

Every connector state change should also append an `events` row. The event log
is the audit and replay surface; dashboard reads should use the runtime tables.

## Safe-Output Integration

Safe-output calls remain the only durable mutation surface available to role
agents. The connector must consume safe-output records and runtime
notifications; it must not parse free text to create work items, approvals,
handoffs, releases, or document state.

Required handlers:

- `status.reply`: create one delivery record for the bound Teams conversation
  or thread and send the complete Markdown reply.
- `status.progress` and `status.complete`: deliver visible progress only when
  policy says the transition should be human-visible.
- `sponsor.ask_question`: create or continue a human-question conversation,
  record the originating work or conversation binding, and send a DM, channel
  message, group chat message, or thread reply according to the safe-output
  payload and project policy.
- approval and human response tools: render Teams Adaptive Cards or
  Teams-compatible response messages and store normalized responses against the
  bound gate or approval.
- `queue.propose_item`: create or request queue/work proposals with a
  value-based rationale, source conversation refs, suggested owner role, and
  explicit no-work-by-default behavior.
- `document.propose_update`, `decision.record`, `risk.register`, and
  `memory.propose_update`: preserve important conversation outcomes in durable
  project knowledge with source links and classification.
- `consult.request` and `handoff.request`: leave role-to-role routing inside
  the v2 runtime. Teams delivery may post only human-visible summaries after
  the runtime records the consult or handoff.
- release tools: post release or no-deployment status only after matching
  release evidence or disposition is recorded.
- `report.blocked`, delivery failures, permission failures, and compaction
  failures: create connector attention items when human or operator action is
  needed.

Payload validation should grow carefully:

- include optional `conversation_id`, `thread_binding_id`, `delivery_target`,
  `target_participant_id`, `target_channel_id`, `visibility_scope`,
  `source_ref`, `classification`, `response_contract_id`, and `correlation_id`
  fields where the current safe-output schema can accept them
- reject claims in `status.reply` that a Teams message, approval, work item,
  release, handoff, document update, or deployment succeeded unless the
  matching runtime event or delivery record exists
- preserve current role-scoped safe-output authorization
- require source conversation refs for proactive work proposals or context
  promotions derived from Teams

## Teams Adapter Responsibilities

The Teams adapter owns Teams-specific protocol details and nothing else.

Inbound responsibilities:

- validate connector installation and configured project/team/channel binding
- receive Bot Framework, Graph subscription, polling, or emulator events
- reject events outside configured projects, teams, chats, and channels
- normalize users, bots, roles, messages, mentions, attachments, cards,
  external ids, timestamps, and permalinks
- detect role aliases, role bot identities, team-wide triggers, and thread
  relationships
- redact obvious secret-like values before logs, traces, prompt context,
  summaries, or role memory can receive them
- create idempotency receipts before routing side effects
- return existing conversation events on duplicate receives

Outbound responsibilities:

- render Teams-compatible Markdown without artificial truncation
- send DMs, channel messages, thread replies, group chat messages, card
  messages, and card updates
- support separate visible role identities according to the configured Teams
  identity model
- update delivery records with Teams message ids and failure details
- classify transient, permanent, permission, formatting, throttling, and
  unknown outcomes
- support policy-approved retry without duplicating successful sends

The adapter must fail closed when consent, installation, permission, or binding
validation fails. It should create an attention item rather than silently
dropping a Teams event or pretending delivery succeeded.

## Role Identity Configuration

Runtime role identity and Teams presentation identity must stay separate.

Project connector config should declare:

- `connector_id`
- Teams tenant id and project team id
- default `project` channel binding
- optional feature, epic, incident, approval, and role channel bindings
- role identity bindings by `role_id`
- role display names and aliases
- mention handles or Teams app/bot ids
- selected identity model: separate app registrations, shared gateway app, or
  hybrid
- allowed human users or groups
- sponsor, operator, approver, release approver, and risk accepter mappings
- team-wide trigger phrase or mention
- relevance threshold and exception policy
- retention defaults for private DMs, project-channel messages, compacted
  summaries, delivery records, and idempotency receipts
- external base URL for status links

Humans normally address a role, not a specific role instance. The runtime
chooses an eligible role service or queue target. Role instance ids belong in
audit records, status pages, and telemetry, not normal Teams conversation.

Separate visible role identities are a product target. The implementation may
start with a shared gateway in the foundation slice only if configuration,
data model, tests, and deployment notes preserve a clean path to separate app
or bot registrations.

## Conversation Ingestion And Routing

Inbound processing order:

1. receive the Teams event and assign a correlation id
2. validate connector, tenant, project, team, channel, chat, and installation
3. compute the inbound idempotency key
4. create or reuse the `external_event_receipts` row
5. map participants, authorities, and configured role identities
6. create or continue the conversation record
7. append the conversation event
8. classify route type
9. create a role assignment, shared context record, relevance work request,
   bound human response, approval response, promotion candidate, or attention
   item
10. append route and dashboard events

Routing rules:

- Direct message to role: continue or create a private role conversation and
  assign the addressed role. Do not create queue or work items by default.
- Agent-initiated human question reply: bind the reply to the originating
  safe-output call, question, work item, approval, risk, or document context.
- Unmentioned project-channel message: append shared project context. Do not
  wake every role. Do not send acknowledgements.
- Feature or epic channel message: append scoped project context using the
  configured focus channel binding.
- Role mention in channel: append shared context and create a focused
  assignment for each configured mentioned role. Replies bind to the same
  thread.
- Team-wide trigger: create a team-wide prompt record, request relevance checks
  from configured roles, and deliver only material role responses.
- Approval or response-card submission: validate authority, normalize the
  response, bind it to the original gate or question, and continue the runtime
  flow through safe-output or response handling.
- Human loop-in: record added participants and route changes while preserving
  the source binding.
- Unknown role mention: do not guess. Record an attention item and optionally
  send a concise clarification when policy allows.
- Ambiguous thread binding: store the event, mark the conversation as needing
  operator attention, and avoid mutating work state.

The connector may create role assignments directly only as runtime input. Any
durable work creation, promotion, handoff, consult, approval, release, or
document update still requires safe-output or existing runtime APIs.

## Context Capture And Compaction

Project and focus-channel messages are shared project context. Direct messages
are private by default. The connector must preserve this distinction during
retrieval, compaction, promotion, dashboard display, and retention expiry.

Required behavior:

- store normalized conversation events with visibility, classification,
  provenance, and retention class
- make unmentioned project and focus-channel messages discoverable to later
  role runs according to scope and policy
- exclude private DM text from shared project context unless promotion is
  explicitly recorded
- compact channel history into source-linked summaries before raw retention
  expiry where policy requires it
- preserve important decisions, requirements, risks, constraints, approvals,
  instructions, blockers, and release facts in the document library, work-item
  dossier, risk register, decision record, or source-linked role memory
- record compaction outputs, source message refs, redactions, classification,
  target artifact refs, and failures
- avoid laundering casual conversation into durable state; durable outcomes
  still require safe-output or document lifecycle actions

## Delivery Handling

Delivery must be visible and recoverable.

Required delivery states:

- `pending`
- `sending`
- `sent`
- `retry_scheduled`
- `failed_transient`
- `failed_permanent`
- `unknown`
- `superseded`
- `canceled`

Delivery flow:

1. create a `delivery_records` row before attempting Teams send
2. resolve destination from conversation binding and safe-output payload
3. validate visibility scope and participant authority
4. render Markdown, card payload, or card update
5. call the Teams adapter with the configured role identity
6. record external Teams message id or failure metadata
7. append delivery event
8. create or clear connector attention items

Unknown outcomes require care. If Teams times out after accepting a message,
the connector should mark the delivery `unknown` and surface it for operator
review unless the adapter can confirm the final Teams message id. Retrying an
unknown outcome may duplicate a human-visible message.

The runtime should not claim a human received a question, reply, approval, or
status update unless the delivery record is `sent` or the failure is explicitly
reported.

## Idempotency

Inbound idempotency key:

- connector id
- Teams tenant id
- external chat, channel, or conversation id
- Teams message id
- event type
- edit or version marker when available

Duplicate inbound events must reuse the existing receipt and conversation
event. They must not create duplicate role assignments, queue items, work
items, relevance checks, safe-output responses, replies, questions, approvals,
promotions, or delivery records.

Outbound idempotency key:

- source safe-output call id or runtime notification id
- destination conversation or channel id
- selected role identity
- delivery purpose
- retry sequence or supersession id

The same safe-output call should produce at most one successful Teams message
per intended destination unless a delivery is explicitly superseded.

## Dashboard Visibility

Extend the existing status snapshot and HTML dashboard to include connector
health without building a separate advanced control-plane UI first.

Add status JSON fields:

- connectors by project, type, setup health, permission health, and last
  activity
- configured role identities and channel bindings
- active conversations by type, visibility, status, and bound work/question
- recent conversation events with body previews redacted by visibility policy
- delivery records by status, destination type, source safe-output call, retry
  count, and error class
- relevance checks by prompt, role, score, threshold, decision, and no-op
  reason
- context summaries, promotions, compaction failures, and retention attention
- connector attention items by owner, reason class, next action, and
  retryability
- duplicate inbound events suppressed
- ambiguous thread bindings

Add HTML sections:

- Connector Health
- Role Identities
- Conversations
- Relevance Checks
- Context Summaries And Promotions
- Delivery Failures
- Connector Attention

The dashboard must not leak private DM body text to unauthorized viewers. Until
authorization-aware dashboard access exists, private body previews should be
omitted or redacted in the default status page.

## Implementation Sequence

Implement the connector through feature stories in the order below. Each story
should be reviewable independently and should leave the runtime in a coherent
state. Avoid combining real Teams credential work with unrelated schema,
routing, dashboard, and compaction changes in one PR.

1. Connector foundation and local test adapter.
2. Direct messages and `status.reply`.
3. Project-channel capture and role mention routing.
4. Agent-initiated human questions and thread binding.
5. Delivery records, retries, failure attention, and outbound idempotency.
6. Role identity deployment model and separate visible role identities.
7. Feature, epic, incident, and focused-work channels.
8. Team-wide relevance checks and no-op recording.
9. Proactive work proposals and conversation promotion.
10. Approval and human-response cards.
11. Context compaction, retention, and durable knowledge preservation.
12. Permission, consent, installation, and authority hardening.
13. Dashboard and observability completion.
14. Release, deployment, rollback, and dogfood validation.

## Feature Story Breakdown

### Story 1 - Connector Foundation And Local Test Adapter

Dependencies: current v2 SQLite runtime, event log, safe-output records,
`RoleService`, and status server.

Acceptance criteria:

- connector-neutral models and repositories exist for connectors,
  participants, conversations, conversation events, receipts, deliveries,
  thread bindings, and attention items
- additive schema migration creates the foundation tables
- connector config fixture validates one project, one default `project`
  channel, role aliases, sponsor/operator mappings, retention settings, and
  external base URL
- local Teams test adapter can replay DM, channel, role mention, duplicate,
  thread reply, and send-failure events
- status JSON exposes connector health and foundation table counts

Release/test notes:

- release as an internal foundation slice only
- run repository, migration, config validation, fixture replay, and status JSON
  tests
- no real Teams tenant permissions are required

### Story 2 - Role Direct Messages And `status.reply`

Dependencies: Story 1.

Acceptance criteria:

- direct message to a configured role creates or continues a private role
  conversation
- role assignment receives normalized conversation context
- `status.reply` creates one delivery record and sends one complete Markdown
  reply through the adapter
- ordinary DM conversation creates no queue item or work item by default
- duplicate DM event does not create duplicate replies or assignments
- private DM body previews are redacted from default status output

Release/test notes:

- cover QA direct-message and no-work-by-default scenarios
- include negative evidence that queue/work tables are unchanged for ordinary
  DMs

### Story 3 - Project Channel Capture And Role Mentions

Dependencies: Stories 1 and 2.

Acceptance criteria:

- unmentioned configured project-channel messages are stored as shared context
  without waking every role
- role mentions resolve only through configured aliases or identities
- mentioned role receives a focused assignment bound to the source thread
- multiple configured role mentions create explicit assignments for each role
- unknown mentions create attention items and do not guess a route

Release/test notes:

- cover project-channel context and role mention QA scenarios
- verify no low-value acknowledgements are posted for unmentioned context

### Story 4 - Agent-Initiated Human Questions And Thread Binding

Dependencies: Stories 1 through 3.

Acceptance criteria:

- `sponsor.ask_question` can target DM, channel, group chat, or existing thread
  according to payload and policy
- delivery records bind the outbound question to the originating work,
  conversation, approval, risk, or document context
- sponsor replies in the Teams thread bind back to the original question
- human loop-in records added participants and preserves the originating
  runtime binding
- ambiguous replies create attention items and do not mutate work state

Release/test notes:

- cover QA agent-initiated question, sponsor-answer binding, and human loop-in
  scenarios
- test focused DM and broader channel/group routes

### Story 5 - Delivery, Retry, Failure Attention, And Idempotency

Dependencies: Stories 1 through 4.

Acceptance criteria:

- delivery records support pending, sending, sent, retry scheduled, transient
  failure, permanent failure, unknown, superseded, and canceled states
- transient Teams failures retry under policy without duplicate successful
  sends
- unknown outcomes are visible for operator review
- failed direct-message or channel delivery is visible in status/dashboard
  output with next action
- status replies cannot claim human delivery succeeded when delivery failed

Release/test notes:

- cover duplicate safe-output delivery, transient failure, permanent failure,
  unknown outcome, and retry tests
- include event log evidence for send attempts and outcomes

### Story 6 - Separate Visible Role Identities

Dependencies: Stories 1 through 5 and security review of identity model.

Acceptance criteria:

- project config supports separate app/bot registrations, shared gateway, or
  hybrid role identity models
- each configured role can have a distinct Teams display name, alias, mention
  handle, and external app/bot id
- outbound delivery uses the configured role identity where the selected Teams
  model supports it
- runtime authorization never relies on display name alone
- emergency disablement can disable one role identity or the connector binding

Release/test notes:

- validate with emulator fixtures first, then a real tenant smoke test when
  consent is approved
- record chosen identity model and limitations in release evidence

### Story 7 - Feature, Epic, Incident, And Focused-Work Channels

Dependencies: Stories 1 through 6.

Acceptance criteria:

- connector config supports focus channel bindings with project identity,
  scope type, source channel id, visibility, and optional work scope
- focus-channel messages are captured as scoped project context
- focus-channel role mentions and threads follow the same routing and delivery
  rules as the default project channel
- status links and dashboard views show source channel scope
- private Teams channels require explicit binding and permission validation

Release/test notes:

- cover QA focus-channel narrowing scenario
- release manual channel configuration first; runtime channel creation can be a
  later enhancement if still desired

### Story 8 - Team-Wide Relevance Checks

Dependencies: Stories 1 through 7 and prompt guidance availability.

Acceptance criteria:

- configured team-wide trigger creates a `team_wide_prompt` record
- every configured role receives a lightweight relevance assignment
- relevance checks record score, threshold, decision, reason, no-op, exception
  reason, and resulting safe-output or delivery refs
- only roles with material specialist input or justified exceptions post Teams
  replies
- role-to-role follow-up still uses runtime consult or handoff, not Teams
  mentions

Release/test notes:

- cover QA relevance, non-relevant silence, below-threshold exception, and
  no-bot-to-bot-transport scenarios
- include regression tests for channel-noise prevention

### Story 9 - Proactive Work Proposals And Conversation Promotion

Dependencies: Stories 1 through 8 and queue/work proposal APIs.

Acceptance criteria:

- role can propose durable work from conversation through safe-output with
  title, summary, source refs, rationale, urgency, suggested owner, and work
  type
- successful proposal stores Teams source refs and classification
- DM promotion records what was promoted, who or what initiated it, target
  artifact, classification, redaction, and source conversation
- connector does not infer work items from free text
- Teams reply reports only the proposal or work that actually exists

Release/test notes:

- cover proactive work proposal, low-value no-proposal, and private DM
  promotion privacy tests
- include fake-claim safe-output regression coverage

### Story 10 - Approval And Human-Response Cards

Dependencies: Stories 1 through 9 and response-contract schemas.

Acceptance criteria:

- approval and human-response safe-output calls render Teams Adaptive Cards or
  structured Teams messages
- card submissions normalize responses and bind them to the originating gate,
  approval, question, work item, risk, or release decision
- sponsor, approver, release approver, and risk accepter authority is enforced
  before accepting privileged responses
- submitted cards update visibly where Teams supports it
- card delivery or submission failure creates attention items

Release/test notes:

- cover approval card, unauthorized approval, stale card, and submission
  failure tests
- record Teams client or tenant limitations in release evidence

### Story 11 - Context Compaction, Retention, And Durable Knowledge

Dependencies: Stories 1 through 10 and security retention decisions.

Acceptance criteria:

- raw private DM, project-channel, focus-channel, delivery, and receipt
  retention policies are configurable
- compaction preserves source-linked decisions, requirements, risks,
  constraints, approvals, instructions, blockers, and release facts
- private DM compaction remains private unless explicit promotion exists
- durable outcomes are written to the document library, work-item dossier,
  source-linked role memory, risk register, decision record, or release
  evidence through safe-output or document lifecycle actions
- retention expiry preserves metadata and hashes needed for audit where policy
  requires it

Release/test notes:

- cover QA retention, compaction, privacy, and durable decision survival tests
- include a manual evidence package for raw history expiry behavior

### Story 12 - Permission, Consent, Installation, And Authority Hardening

Dependencies: Stories 1 through 11 and final Teams substrate choice.

Acceptance criteria:

- connector startup validates app installation, consent, team binding, channel
  binding, role identity binding, member metadata access, and send capability
- setup/admin permissions are documented separately from runtime permissions
- broad Graph permissions require explicit documented approval
- missing or revoked permission fails closed and creates actionable status
- human authority maps from configured people records or groups, not display
  names or free-text mentions
- connector credential values never appear in config, logs, traces, prompts,
  memory, or artifacts

Release/test notes:

- cover permission failure, revoked permission, unauthorized sponsor/operator,
  and credential redaction tests
- include tenant consent evidence before real Teams release

### Story 13 - Dashboard And Observability Completion

Dependencies: Stories 1 through 12.

Acceptance criteria:

- status JSON and HTML show connector health, role identities, channels,
  conversations, deliveries, relevance checks, context summaries, promotions,
  permission checks, and attention items
- private body text is redacted unless an authorization-aware view explicitly
  permits it
- OpenTelemetry spans cover receive, idempotency, identity mapping,
  conversation append, route classification, role wake, relevance, delivery,
  retry, response binding, compaction, and permission validation
- metrics cover inbound events, duplicates suppressed, active conversations,
  delivery failures, relevance decisions, ambiguous bindings, private DM
  promotions, and compaction counts

Release/test notes:

- cover status JSON contract tests, HTML smoke tests, and telemetry smoke tests
- include redaction evidence in QA handoff

### Story 14 - Release, Deployment, Rollback, And Dogfood Validation

Dependencies: Stories 1 through 13.

Acceptance criteria:

- project-scoped Compose deployment can run the connector service with the v2
  runtime and configured database
- real Teams deployment profile documents app registration, consent,
  installation, secret binding, role identities, project team/channel bindings,
  retention, external base URL, and disablement path
- smoke tests prove inbound DM, outbound reply, project-channel capture, role
  mention, team-wide relevance, agent question, approval response, delivery
  failure, and permission failure behavior
- rollback can disable the connector without deleting runtime state,
  conversation records, delivery records, or audit events
- release evidence links product, architecture, security, prompt, engineering,
  QA, and release artifacts

Release/test notes:

- release through the normal v2 release flow
- capture smoke evidence and known residual risks before declaring the Teams
  connector complete

## Tests To Add

Unit and repository tests:

- schema migration creates all connector tables
- participant upsert is stable across repeated Teams events
- inbound receipt prevents duplicate side effects
- conversation creation preserves visibility scope, classification, bindings,
  and external ids
- thread binding lookup follows the architecture order
- delivery records transition through all delivery states
- relevance checks persist score, threshold, decision, reason, no-op, and
  exception fields
- context summaries and promotions preserve source refs and visibility
- permission checks fail closed when required bindings are missing
- private DM previews are redacted from default status output

Routing tests:

- DM to Product Manager creates a private conversation and one role assignment
- ordinary DM reply uses `status.reply` and creates no queue or work item
- unmentioned project-channel message is captured as context and wakes no role
- focus-channel message is scoped to the configured feature or epic
- channel role mention routes only to the mentioned role
- multiple configured role mentions create explicit assignments for each role
- unknown mention creates an attention item and no guessed route
- team-wide trigger records relevance checks and delivers only material replies
- sponsor answer in a bound thread resolves to the original question context
- approval submission resolves to the correct gate and validates authority
- ambiguous thread reply is stored but does not mutate work state

Safe-output and delivery tests:

- `status.reply` creates one delivery record and sends complete Markdown
- duplicate `status.reply` processing does not send a second Teams message
- `sponsor.ask_question` creates a delivery record with source work binding
- approval or response card creates delivery and response bindings
- proactive work proposal safe-output records Teams source refs
- delivery failure records error class, retry count, next action, and event log
- unknown delivery outcome is visible and not automatically retried as success
- status replies cannot claim Teams delivery succeeded when delivery failed

Security and privacy tests:

- event outside configured team/channel is rejected or ignored with attention
- untrusted human cannot be treated as sponsor or operator by display name
- private DM content is not included in project-channel context
- promoted work stores only the allowed source reference and classification
- secret-like values are redacted from logs and status previews
- missing consent or binding fails closed and produces actionable status
- unauthorized human cannot approve gates or accept risk
- private channel capture requires explicit binding and permission validation

QA scenario coverage:

- map tests to the QA BDD scenarios for DMs, no-work-by-default,
  project-channel context, focus channels, role mentions, team-wide relevance,
  agent-initiated questions, human loop-in, proactive work proposals,
  approval cards, idempotency, delivery failure, permission failure, privacy,
  retention, compaction, and thread binding
- record any story-level deferrals as release risks with owners and target
  story numbers

## Deployment Considerations

Local deployment:

- run connector service beside the v2 runtime status service
- use the same SQLite database path as the runtime for local development
- use a Teams emulator or fixture adapter by default for automated tests
- keep connector secrets outside Git and outside project YAML
- expose the runtime external base URL used in Teams status links

Dogfood and enterprise Teams deployment:

- choose and document the Teams API substrate before requesting tenant consent
- separate setup/admin permissions from runtime message processing where
  possible
- validate app installation, consent, team binding, channel binding, role
  identities, and member metadata at startup
- fail closed when permission or binding validation fails
- record consent metadata, app owner, credential rotation path, identity model,
  and emergency disablement path
- keep raw payload retention configurable and distinct from durable project
  records
- emit OpenTelemetry with project id, connector id, conversation id, role id,
  correlation id, and redacted external message ids
- provide rollback by disabling connector ingress and outbound delivery while
  preserving database state and audit evidence

Docker Compose, Terraform, or Helm outputs should remain project-scoped when
deployment files are eventually added. This implementation plan does not
change deployment manifests.

## Risks

| Risk | Engineering response |
| --- | --- |
| Teams identity implementation changes after foundation work starts. | Keep runtime identity and connector presentation separate; build against explicit role identity bindings. |
| Broad Teams permissions are requested too early. | Start with emulator/test adapter and bot-context assumptions; require security review before real tenant permissions. |
| Connector code mutates work state from free text. | Route all durable mutations through safe-output and runtime APIs; add regression tests for no-work-by-default. |
| Private DMs leak through status pages, compaction, or context retrieval. | Store visibility scope, redact private previews, require explicit promotions, and test dashboard and compaction output. |
| Duplicate Teams events create duplicate role replies or work proposals. | Require inbound receipts before routing side effects and outbound idempotency per safe-output call. |
| Unknown delivery outcomes produce duplicate Teams messages. | Mark unknown outcomes visibly and require confirmation or policy before retry. |
| Role services become Teams-aware. | Keep Teams mapping in connector modules and pass normalized assignments into the role runtime. |
| Status dashboard becomes unreadable as connector data grows. | Add compact connector sections first and reserve advanced drilldown for later control-plane UI. |
| Team-wide relevance checks silence important input. | Record scores and justifications, allow justified exceptions, and add QA coverage for missed-response risk. |
| Compaction loses important decisions or misclassifies private content. | Preserve source-linked durable records, keep private compaction private, and test before retention expiry. |
| Real tenant permissions differ from emulator behavior. | Gate release on tenant smoke tests and permission evidence. |

## Open Decisions

- OD-ENG-001: Which Teams substrate is first for real tenant integration:
  Bot Framework, Graph subscriptions, polling, or a hybrid?
- OD-ENG-002: Does the connector add tables through a schema version 2
  migration in `db.py`, or introduce a migration runner before expanding the
  schema further?
- OD-ENG-003: Should the current `conversations` table be migrated in place or
  replaced by additive connector conversation tables while preserving backward
  compatibility for existing demos?
- OD-ENG-004: What exact payload fields should be added to `status.reply`,
  `sponsor.ask_question`, approvals, and queue proposal safe-output calls for
  conversation binding without breaking existing safe-output tests?
- OD-ENG-005: What is the first real role identity deployment model: one
  gateway app with role aliases, separate app registrations, or hybrid?
- OD-ENG-006: What status-page authorization model is required before private
  DM body text can ever be shown outside local development?
- OD-ENG-007: Should queue/work proposal creation be implemented before Teams
  source linking, or should Teams initially record successful proposal
  safe-output calls until queue proposal APIs are complete?
- OD-ENG-008: What retry policy should be used for transient send failures and
  how should operators resolve unknown delivery outcomes?
- OD-ENG-009: Which approval and human-response templates need Adaptive Cards
  first?
- OD-ENG-010: What exact raw-body deletion mechanism should each storage
  adapter use after retention expiry?
- OD-ENG-011: Should runtime-created feature or epic channels be supported
  after manual channel binding is stable?

## QA Handoff

Engineering should hand QA evidence per feature story and for the final
connector release.

Per-story evidence:

- implemented story id and linked source documents
- schema/config changes included in the story
- automated test results and any manual evidence
- status JSON or dashboard screenshots where visibility changed
- event log samples for receive, route, safe-output delivery, duplicate
  suppression, relevance, compaction, permission checks, or ambiguous binding
- known residual risks and follow-up story numbers

Final connector evidence:

- schema migration test output and list of connector tables
- connector config fixture for the `agentic-mesh-dev` project
- Teams emulator or fixture payloads for DM, project channel, focus channel,
  role mention, team-wide prompt, duplicate event, thread reply, approval, and
  send failure scenarios
- real Teams tenant smoke evidence when permissions are approved
- status JSON samples showing connector health, role identities,
  conversations, relevance checks, context summaries, delivery failures,
  duplicate suppression, permission checks, and attention items
- safe-output records proving `status.reply`, `sponsor.ask_question`,
  proactive work proposals, approvals, context promotions, and release/status
  deliveries were created from role runs
- negative evidence showing ordinary DMs and unmentioned channel messages did
  not create queue items or work items
- privacy evidence showing private DM body previews are redacted from the
  default dashboard and excluded from shared compaction
- retention/compaction evidence showing important decisions survive raw history
  expiry with provenance
- deployment, rollback, permission, and release evidence for the selected
  Teams identity model

QA should treat Story 1 as connector-spine validation only. The Teams connector
should not be called complete until the full feature-story set is implemented
or explicitly reduced by sponsor decision with recorded risks and owners.

## Review Log

- RL-001 | engineering | downstream-implementation-plan | full document |
  Created an implementation-ready Teams connector plan from product, UX,
  architecture, security, prompt-engineering, QA, and v2 runtime reset source
  documents. The plan defines the MVP, service boundaries, schema changes,
  safe-output integration, Teams adapter responsibilities, routing, delivery,
  idempotency, dashboard visibility, implementation sequence, tests,
  deployment considerations, risks, open decisions, and QA handoff. |
  incorporated 2026-06-12
- RL-002 | engineering | sponsor-correction | full document | Sponsor
  corrected the implementation framing: the plan must target the complete
  Teams connector product/design, delivered through ordered feature
  stories/slices, rather than presenting the smallest MVP as the endpoint. |
  incorporated 2026-06-12
