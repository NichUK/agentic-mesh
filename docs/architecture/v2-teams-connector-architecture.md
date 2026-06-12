# V2 Teams Connector Architecture

Status: draft downstream architecture

Owner role: solution-architect

Date: 2026-06-12

Source documents:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`
- `docs/architecture/v2-runtime-reset.md`

## Purpose

This document defines the v2 Teams connector architecture for Agentic Mesh.
It translates the product definition into connector, identity, conversation,
database, idempotency, delivery, safe-output, and generalization requirements
for downstream security, prompt, engineering, QA, and release work.

The design goal is to make Microsoft Teams a natural human collaboration
surface for role specialists while preserving the v2 runtime as the source of
truth for state, routing, safe-output authority, audit, and lifecycle flow.

## Core Position

Teams is a collaboration connector, not the orchestration engine.

Teams must not be used as agent-to-agent transport. Role-to-role consults,
handoffs, blockers, lifecycle transitions, relevance decisions, approvals,
work promotion, release decisions, and closure must flow through v2 runtime
queues, safe-output calls, and database state. Teams may expose human-visible
messages, summaries, questions, approvals, status links, and discussion
context, but it is not the bus by which agents coordinate with one another.

## Service Boundary

The Teams connector is a runtime service that sits between Microsoft Teams and
the v2 runtime kernel.

Responsibilities:

- receive Teams webhook, Bot Framework, Graph, or polling events
- normalize inbound Teams messages into conversation events
- map Teams users, channels, threads, and role identities to runtime records
- route direct messages, role mentions, channel context, team-wide prompts,
  sponsor replies, and approval responses into the v2 runtime
- deliver runtime replies, human questions, approval cards, and status updates
  back to Teams
- record delivery status, retries, permanent failures, and external message ids
- emit OpenTelemetry spans, metrics, and logs with runtime correlation ids

Non-responsibilities:

- deciding lifecycle progression
- creating work items without an explicit safe-output request
- interpreting free text as durable state mutation
- mediating role disagreements
- performing role-to-role consults or handoffs through Teams mentions
- treating Teams message history as canonical project memory

The connector can be implemented as one service that supports multiple role
identities. Whether those identities are separate Teams app registrations,
multiple bot identities, or one gateway app with role-addressable presentation
is an implementation decision, not a runtime semantic dependency.

## Connector Flow

Inbound flow:

```text
Teams event
  -> connector receive
  -> external event idempotency check
  -> identity/channel/thread mapping
  -> conversation event append
  -> routing classification
  -> v2 runtime message, context record, or safe-output response binding
  -> optional role-service wake
```

Outbound flow:

```text
safe-output call or runtime notification
  -> connector delivery request
  -> visibility and destination resolution
  -> Teams message, reply, DM, card, or thread update
  -> delivery status record
  -> event log and dashboard read model update
```

Inbound messages are stored before role processing so the runtime can audit
what was received even when downstream routing or delivery fails. Outbound
delivery is also recorded before send attempts so retries and failures are
visible.

## Role Identity Model

Agentic Mesh separates runtime role identity from connector presentation.

Runtime identities:

- `project_id`: the project network, for example `agentic-mesh-dev`
- `role_id`: stable role template or configured role, for example
  `product-manager`
- `role_instance_id`: concrete role service, for example
  `agentic-mesh-dev.product-manager.1`
- `role_service_id`: runtime process or lease identity for claiming work
- `connector_identity_id`: configured Teams-facing identity or alias

Teams-facing role identity should make the role feel separately addressable to
humans. The connector must support:

- role display name
- role aliases and mention handles
- optional per-role bot/app identity
- optional shared gateway identity with role disambiguation
- project-scoped role binding so the same role name can exist in many Teams
- status links that identify the project, role, and relevant work context

The runtime must never infer role authority from a Teams display name alone.
Connector identity maps must be configured and versioned with the project or
connector binding. A Teams identity resolves to a runtime role only when the
binding is explicit and active.

A role mention in Teams routes to a role service or role queue. It does not
address another bot for an agent-to-agent exchange.

## Human And Participant Model

The connector normalizes every participant into a runtime participant record.

Participant categories:

- `human`: a Teams user who can converse with roles
- `sponsor`: a human with project steering or approval authority where
  configured
- `operator`: a human with connector or runtime operational authority
- `role`: configured Agentic Mesh role identity
- `role_instance`: optional concrete role service identity for audit
- `external_service`: non-human integration where supported later

Required participant attributes:

- runtime participant id
- connector type, initially `teams`
- Teams tenant id
- Teams user, bot, app, or conversation member id
- display name as observed
- configured authority labels, if any
- project membership or allowed project scopes
- last observed timestamp

Human authority is policy-backed. Being present in a Team or chat does not by
itself grant sponsor, operator, approval, or release authority.

## Conversation Model

A conversation is a runtime record that binds people, roles, source messages,
visibility, and optional work context.

Conversation types:

- `role_dm`: direct human-to-role conversation
- `agent_question_dm`: role-initiated direct question to a human
- `project_channel`: shared project-room context
- `feature_channel`: shared context scoped to a feature, epic, incident, or
  focused initiative
- `role_mention_thread`: channel thread that asks a specific role to respond
- `team_wide_prompt`: shared prompt that asks all roles to run relevance checks
- `approval_thread`: conversation bound to an approval or human response gate
- `work_thread`: conversation bound to a queue item, work item, risk, or
  document update

Core conversation fields:

- `conversation_id`
- `project_id`
- `connector_id`
- `connector_type`
- `source_type`
- `visibility_scope`
- `external_tenant_id`
- `external_team_id`
- `external_channel_id`
- `external_chat_id`
- `external_thread_id`
- `root_external_message_id`
- `current_status`
- `bound_queue_item_id`, nullable
- `bound_work_item_id`, nullable
- `bound_safe_output_call_id`, nullable
- `bound_approval_id`, nullable
- `bound_document_ref`, nullable
- `created_at`, `updated_at`, `last_message_at`

Conversation events store the message-level facts:

- `conversation_event_id`
- `conversation_id`
- `external_message_id`
- `external_reply_to_id`, nullable
- sender participant id
- mentioned participant ids
- normalized Markdown or text body
- attachments and card payload metadata
- event kind
- event timestamp
- source permalink, when available
- redaction and retention class
- correlation id

The raw connector payload may be retained for a configurable period, but the
runtime read model must not depend on raw Teams JSON for normal operation.

## Conversation Routing

Routing is based on configured connector bindings and conversation context.

Direct message to role:

- creates or continues a `role_dm` conversation
- routes to the addressed role
- does not create a queue item or work item by default
- keeps the conversation private to the human and role until promotion

Unmentioned project or feature channel message:

- appends shared context to a `project_channel` or `feature_channel`
  conversation
- does not wake every role by default
- may be compacted into source-linked summaries or document-library updates

Role mention in a channel:

- appends channel context
- routes a focused assignment to the mentioned role
- binds replies to the same Teams thread
- does not invite other roles unless the role uses runtime consult or handoff
  tools

Team-wide prompt:

- creates a `team_wide_prompt` context record
- asks configured roles to run lightweight relevance checks
- records relevance scores, thresholds, and reasons
- posts replies only from roles with material specialist input or an explicit
  justified exception

Sponsor question, approval, or human response:

- binds the Teams reply to the originating safe-output call, approval,
  question, queue item, work item, or document context
- records the normalized human response before the flow continues

Agent-initiated human question:

- originates from a safe-output call or governed runtime request
- targets a human, human group, channel, group chat, role-facing thread, or
  existing conversation
- records audience, reason, source work context, and destination
- preserves the return binding to the originating work or decision context

## Direct-Message Privacy

Direct messages to a role are private by default to that human and addressed
role. They are not shared project context merely because the role belongs to a
project.

Privacy requirements:

- store `role_dm` conversations with a private visibility scope
- restrict dashboard and read-model access to authorized users and services
- exclude private DM text from general project-channel context compaction
- prevent other roles from receiving DM context unless explicitly promoted,
  source-linked, or included through an authorized work item
- record when a DM is promoted into tracked work, memory, document updates, or
  broader discussion

Promotion must be explicit. A role may propose durable work from a DM, but the
durable mutation happens only through safe-output tools. The resulting work
item should cite the DM conversation or selected source messages without
leaking more private content than the target visibility allows.

## Project And Feature Channel Context

A configured project channel is shared project context. Feature, epic,
incident, or focused-work channels are shared context scoped to that body of
work.

The connector should capture channel messages with provenance and make them
discoverable to future role runs according to project retention and visibility
rules. Important decisions, constraints, requirements, risks, instructions,
and approvals should be preserved in the document library, work-item dossier,
or source-linked role memory rather than relying on Teams history alone.

Channel compaction produces summaries with:

- source conversation id
- source message ids or permalinks
- project and feature scope
- author and timestamp provenance
- summary type, such as decision, risk, requirement, instruction, or context
- target document, work item, or role memory reference

Compaction must not turn casual conversation into durable state by inference.
Durable decisions and work updates still require the relevant safe-output or
document lifecycle action.

## Thread Binding

Teams thread replies must remain bound to the original runtime context where
one exists.

Thread bindings are required for:

- role replies in channel mention threads
- sponsor answers to agent questions
- approval and human response gates
- delivery failure follow-up
- work item status discussion
- feature-channel decisions

The connector stores both external Teams thread identifiers and runtime binding
identifiers. When a Teams reply arrives, lookup order should be:

1. external message or thread idempotency key
2. explicit delivery record binding
3. conversation thread binding
4. channel or chat fallback binding
5. unbound project context capture

If binding is ambiguous, the connector should record the event as received,
surface an operational attention state, and avoid mutating work state until a
human or operator resolves the binding.

## Database And Read-Model Needs

The v2 database is the operational source of truth. SQLite is the first backend,
but table and repository boundaries should remain suitable for Postgres.

Required logical records:

- `connectors`: configured connector instances per project or deployment
- `connector_role_identities`: Teams-facing role identities and aliases
- `connector_channels`: project, feature, approval, and role channel bindings
- `participants`: normalized humans, roles, role instances, and services
- `participant_authorities`: sponsor, operator, approver, or other labels
- `conversations`: durable conversation bindings and visibility scopes
- `conversation_participants`: participants in each conversation
- `conversation_events`: normalized inbound and outbound message events
- `conversation_context_summaries`: compacted, source-linked summaries
- `delivery_records`: outbound send attempts, status, retries, and message ids
- `external_event_receipts`: idempotency receipts for inbound Teams events
- `relevance_checks`: team-wide prompt checks by role and result
- `thread_bindings`: Teams thread ids bound to runtime objects
- `connector_attention_items`: unresolved failures, permission issues, or
  ambiguous routing requiring human or operator action

Dashboard/read-model views should answer:

- which conversations are active, private, shared, waiting, or failed
- whether a Teams reply is bound to a work item, approval, question, or queue
  item
- which connector deliveries failed and what the next action is
- which team-wide prompts were evaluated and which roles stayed silent
- which channel messages were compacted into durable project knowledge
- which DMs were promoted into tracked work or document updates

The event log records append-only audit events for receive, route, relevance
check, safe-output dispatch, delivery attempt, retry, failure, compaction, and
promotion.

## Idempotency

The connector must tolerate duplicate inbound events and repeated outbound
delivery attempts.

Inbound idempotency keys should include:

- connector id
- Teams tenant id
- Teams conversation, channel, or chat id
- Teams message id
- event type
- message edit or version marker when available

The first successful receipt creates an `external_event_receipts` record and
the normalized conversation event. Duplicate receives return the existing
conversation event and must not create duplicate queue items, work items,
safe-output responses, relevance checks, or role replies.

Outbound idempotency keys should include:

- safe-output call id or runtime notification id
- destination conversation or channel id
- delivery purpose
- retry sequence

For safe-output-generated replies, the same safe-output call should produce at
most one successful Teams message per intended destination unless the delivery
record is explicitly superseded or retried after an unknown outcome. Unknown
outcomes should be visible in the dashboard because they may require a human
decision before resending.

## Delivery Failures

Connector delivery is a first-class runtime concern. A role reply is not fully
complete from the human user's perspective until Teams delivery has succeeded
or a visible failure has been recorded.

Delivery states:

- `pending`
- `sending`
- `sent`
- `retry_scheduled`
- `failed_transient`
- `failed_permanent`
- `unknown`
- `superseded`
- `canceled`

Failure records should capture:

- target conversation or channel
- source safe-output call or runtime notification
- connector error code and class
- retry count and next retry time
- whether the role run can continue
- whether a human or operator must act
- fallback link to the runtime status surface

Transient failures may retry under policy. Permanent failures should create a
connector attention item and appear on the v2 dashboard. The runtime should not
claim that a human was asked, an approval was delivered, or a status reply was
visible unless delivery status supports that claim.

## Safe-Output Integration

Safe-output calls remain the only durable mutation surface for agents. The
Teams connector consumes safe-output records and runtime notifications; it
does not parse free text to mutate work state.

Required integrations:

- `status.reply`: deliver complete Markdown replies to the bound Teams
  conversation or thread
- `sponsor.ask_question`: create or continue a human question conversation and
  bind replies to the originating context
- approval and human response tools: render Teams cards or messages and record
  normalized responses
- proactive work proposal tools: create or request queue/work items with a
  value-based rationale and source conversation links
- consult and handoff tools: record runtime role-to-role flow, optionally
  posting human-visible summaries without using Teams as the transport
- blocker and attention tools: notify appropriate humans or channels when
  visible escalation is required
- release and deployment tools: post status only after the matching release
  evidence or disposition is recorded

The connector must validate that a delivery request is authorized for its
conversation and visibility scope. For example, a private DM reply must not be
redirected into a project channel unless a safe-output action or human action
explicitly promotes it.

## Relevance Checks

Team-wide prompts should run relevance checks without creating channel noise.

A relevance check records:

- prompt conversation id
- role id and optional role instance id
- relevance score
- configured threshold
- decision: `respond`, `stay_silent`, or `respond_with_exception`
- reason
- resulting safe-output call or no-op event

Relevance checks are runtime work, not Teams bot-to-bot conversation. Roles
evaluate the prompt through their role service context and safe-output contract.
Only selected role replies are delivered to Teams.

## Observability

The connector must emit OpenTelemetry traces, logs, and metrics.

Required spans:

- connector receive
- idempotency lookup
- identity mapping
- conversation append
- routing classification
- role wake request
- safe-output delivery request
- Teams send attempt
- Teams delivery retry
- response binding
- compaction

Required metrics:

- inbound Teams events by project and source type
- duplicate inbound events suppressed
- active conversations by visibility and type
- delivery successes and failures
- delivery latency
- relevance checks by role and decision
- ambiguous thread bindings
- private DM promotions
- channel compaction count

Logs must include correlation id, project id, connector id, conversation id,
role id where applicable, external message id where applicable, and redacted
failure metadata.

## Generalization Beyond Teams

The connector architecture must generalize to Slack, GitHub Issues, Azure
DevOps, email, web console, and CLI input surfaces.

Connector-neutral concepts:

- participant
- role identity binding
- conversation
- conversation event
- visibility scope
- external thread binding
- delivery record
- human response
- approval
- source-linked context summary
- idempotency receipt

Teams-specific concepts such as Team, channel, chat, Bot Framework activity,
Adaptive Card, and Graph message id should be stored as connector metadata
behind these neutral records. Slack can map workspaces, channels, DMs, threads,
Block Kit, and timestamps into the same model. GitHub Issues can map issue
comments, mentions, labels, and review threads into the same model. Azure
DevOps can map work-item comments and discussion threads. Email can map
mailboxes, message ids, reply chains, and recipients.

The runtime should depend on the normalized connector contract, not Teams API
shapes. A role should be able to answer, ask a human, propose work, or bind an
approval through the same safe-output contract regardless of connector.

## Security And Privacy Handoff Notes

Security Architecture should refine:

- minimum Teams and Entra permissions
- app registration and consent model for separate role identities
- tenant and project membership checks
- sponsor and operator authority mapping
- private DM retention and access controls
- raw Teams payload retention defaults
- redaction requirements for logs, traces, and dashboards
- eDiscovery, export, and deletion expectations

Architecture recommendation: treat raw connector payload retention as
operational traceability, not canonical knowledge. Durable project knowledge
belongs in the document library, work-item dossiers, event log, and
source-linked memory.

## Engineering Slice Recommendation

The smallest useful implementation slice should prove:

- configured connector instance for one project
- role DM ingestion and complete Markdown `status.reply`
- project-channel message capture without waking every role
- role mention routing to one role
- thread binding for role replies
- inbound idempotency for duplicate Teams events
- outbound delivery record with success and failure status
- safe-output delivery through `status.reply` and `sponsor.ask_question`
- dashboard read model for conversations and delivery failures

Team-wide relevance checks, compaction, approval cards, feature-channel
creation, and separate Teams app registrations can follow once the first slice
has stable conversation and delivery records.

## Open Decisions

- OD-001: Choose the Teams role identity implementation: separate app/bot
  registrations, one gateway app with role-addressable presentation, or a
  hybrid model.
- OD-002: Define default retention for raw Teams payloads, normalized
  conversation events, compacted summaries, and source-linked durable
  decisions.
- OD-003: Decide whether feature and epic channels are manually configured in
  the MVP or created by runtime/operator workflow later.
- OD-004: Decide the first Teams API substrate: Bot Framework, Graph
  subscriptions, polling, or a hybrid.
- OD-005: Define the first dashboard surface for connector attention items and
  ambiguous thread bindings.
- OD-006: Confirm which safe-output tool names and schemas will carry
  proactive work proposals, approvals, and human response gates in the current
  v2 implementation.
- OD-007: Decide how much private DM source text may be cited when promoted
  into shared work items or document updates.
- OD-008: Decide whether one role service or a separate lightweight classifier
  performs team-wide relevance checks in the MVP.

## Review Log

- RL-001 | solution-architect | downstream-handoff | full document | Created
  Teams connector architecture from product definition, downstream handoff, and
  v2 runtime reset. The design explicitly keeps Teams out of agent-to-agent
  transport and defines connector identity, conversation, participant,
  database, idempotency, delivery, safe-output, privacy, thread binding,
  project/feature context, and generalization requirements. | incorporated
  2026-06-12
- RL-002 | solution-architect | open-decision | identity implementation |
  Product direction wants separate visible role identities, but Teams
  implementation shape may vary by tenant permissions and operational support.
  | captured as OD-001 for security and engineering review.
- RL-003 | solution-architect | open-decision | retention and privacy |
  Product direction says private DMs stay private until promoted and channel
  context may be compacted, but exact retention and citation rules require
  security architecture input. | captured as OD-002 and OD-007.
