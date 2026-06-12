# V2 Teams Connector Implementation Log

Status: in progress

Owner role: engineering

Date started: 2026-06-12

Source documents:

- `docs/engineering/v2-teams-connector-implementation-plan.md`
- `docs/qa/v2-teams-connector-test-plan.md`
- `docs/architecture/v2-teams-connector-architecture.md`

## Story 1 - Connector Foundation And Local Test Adapter

Status: engineering rework implemented; awaiting QA retest

### Objective

Create the connector-neutral foundation needed before real Teams tenant work:
database tables, repository methods, local Teams-style test adapter, config
validation fixture, idempotent inbound replay, delivery records, attention
items, and status visibility.

### Files Changed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_teams_connector_foundation.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Added connector foundation tables:
  - `connectors`
  - `connector_participants`
  - `external_event_receipts`
  - `conversation_events`
  - `thread_bindings`
  - `delivery_records`
  - `connector_attention_items`
- Added repository methods to configure connectors, participants, receipts,
  conversations, conversation events, thread bindings, delivery records, and
  connector attention items.
- Extended `status_snapshot()` with connector counts, delivery status counts,
  connector records, participants, conversations, events, receipts, deliveries,
  thread bindings, and attention items.
- Added status-page sections for connector health, delivery records, and
  connector attention.
- Added `ConnectorConfig` validation for the local Teams foundation fixture.
- Added `LocalTeamsTestAdapter` that can replay:
  - direct messages
  - project-channel messages
  - configured role mentions
  - duplicate inbound events
  - thread-bound messages
  - unknown role mentions
  - simulated outbound send failures
- Preserved the boundary that Teams is not agent-to-agent transport. Story 1
  records connector events and delivery state only; role consults/handoffs
  remain runtime-safe-output work for later stories.

### Tests Run

Command:

```powershell
pytest -q
```

Result:

```text
19 passed
```

Focused coverage added:

- connector config validation rejects missing fields and unknown retention keys
- local adapter installs connector and role/human participants
- direct message replay records a private role conversation event
- duplicate inbound replay reuses the original receipt
- unmentioned project-channel replay records shared project context
- role mention replay records a focused role route and thread binding
- unknown role mention creates connector attention
- simulated send failure creates a failed delivery and attention item
- empty status snapshot exposes connector foundation fields

### Known Limitations

- No real Microsoft Teams, Bot Framework, Graph, or Entra integration yet.
- No role assignment creation from connector events yet.
- No `status.reply` delivery integration yet.
- No role identity deployment model yet.
- No permission, consent, or tenant validation yet.
- No retention/compaction logic yet.

### Next Engineering Story

Story 2 - Role Direct Messages And `status.reply`.

Do not begin Story 2 until QA has reviewed Story 1 and any required rework has
passed retest.

## Story 2 - Role Direct Messages And `status.reply`

Status: engineering implemented; awaiting QA review

### Objective

Support direct messages to configured role agents as private conversations that
create role assignments without creating queue/work items by default, and route
role `status.reply` safe-output calls to exactly one outbound delivery record.

### Files Changed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/safe_outputs.py`
- `tests/test_v2_teams_connector_direct_messages.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Added `role_assignments` as a durable runtime table for connector-created
  role assignments.
- Added repository methods to create, complete, and list role assignments.
- Direct-message replay now creates a private `direct_conversation` assignment
  when the event targets a configured role.
- Duplicate inbound direct messages reuse the original receipt and do not create
  duplicate role assignments.
- Added `ConnectorSafeOutputService`, which records safe-output calls and
  routes `status.reply` payloads with connector conversation fields through the
  local adapter.
- `status.reply` now creates one sent delivery record in the local adapter path.
- `SafeOutputService.record()` now returns the actual `call_id`, allowing
  connector deliveries to reference the safe-output call that caused them.
- Default `status_snapshot()` redacts private conversation body previews.

### Tests Run

Commands:

```powershell
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py
pytest -q
```

Results:

```text
4 passed
20 passed
```

Focused coverage added:

- direct message to Product Manager creates one private role assignment
- duplicate direct message does not create duplicate assignment
- ordinary DM creates no queue item or work item
- private DM body preview and external receipt payload body are redacted from
  default status snapshot
- role `status.reply` records a safe-output call and creates one sent delivery
  record
- completed role assignment is visible in status snapshot

### QA Rework

QA found that Story 2 initially redacted `conversation_events[*].body_preview`
but still exposed private DM text through
`external_event_receipts[*].payload.body` in the default status snapshot.

Engineering rework:

- added default redaction for private direct-message receipt payload body fields
  in `status_snapshot()`
- preserved raw database storage for future authorized/debug paths
- added regression assertions to
  `tests/test_v2_teams_connector_direct_messages.py`

Retest commands:

```powershell
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py
pytest -q
```

Retest results:

```text
4 passed
20 passed
```

### Known Limitations

- Role assignment claiming is still manual/test-driven; live role-service
  assignment loops are PB-004 scope.
- Real Teams delivery remains later-story scope.
- Delivery retry, duplicate outbound suppression, and unknown outcomes remain
  Story 5 scope.
- Direct-message privacy is currently redacted at default status snapshot level;
  full authorization-aware dashboard access remains later-story scope.

### Next Engineering Story

Story 3 - Project Channel Capture And Role Mentions.

Do not begin Story 3 until QA has reviewed Story 2 and any required rework has
passed retest.

## Story 3 - Project Channel Capture And Role Mentions

Status: engineering implemented; awaiting QA review

### Objective

Capture unmentioned project-channel messages as shared project context without
waking every role, and create focused role assignments only for configured role
mentions.

### Files Changed

- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_project_channels.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Unmentioned project-channel messages continue to record conversation events
  with `visibility_scope = project` and do not create role assignments.
- Configured role mentions create one `channel_role_mention` assignment per
  mentioned role.
- Mention-created assignments preserve source receipt, conversation id, message
  id, thread ref, project visibility, and connector id in payload.
- Duplicate role-mention messages reuse the original receipt and do not create
  duplicate assignments.
- Unknown role mentions create connector attention and do not create guessed
  assignments.

### Tests Run

Commands:

```powershell
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py
pytest -q
```

Results:

```text
7 passed
23 passed
```

Focused coverage added:

- unmentioned channel context creates no role assignment
- project-channel body preview remains visible as shared project context
- configured multi-role mention creates focused assignments for each role
- duplicate role mention does not duplicate assignments
- thread binding is recorded for mentioned role threads
- unknown role mention creates attention and no assignment

### Known Limitations

- Feature/epic/focused-work channel bindings remain Story 7 scope.
- Team-wide relevance checks remain Story 8 scope.
- Role assignment claiming remains PB-004 live role-service scope.
- The local adapter still uses explicit `mentioned_roles` fixtures rather than
  real Teams mention entity parsing.

### Next Engineering Story

Story 4 - Agent-Initiated Human Questions And Thread Binding.

Do not begin Story 4 until QA has reviewed Story 3 and any required rework has
passed retest.

## Story 4 - Agent-Initiated Human Questions And Thread Binding

Status: engineering implemented; awaiting QA review

### Objective

Allow a role to ask a human for clarification or a decision through
`sponsor.ask_question`, deliver the question through the connector, and bind the
resulting Teams thread back to the originating work/question context.

### Files Changed

- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_human_questions.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Added local adapter delivery support for `sponsor.ask_question`.
- `ConnectorSafeOutputService` now routes `sponsor.ask_question` payloads with
  connector conversation fields to the local adapter.
- Agent-initiated questions create sent delivery records with purpose
  `sponsor.ask_question`.
- Agent-initiated questions create `human_question` thread bindings to the
  originating work/question target.
- Human replies in the same Teams thread create `teams_thread` bindings that
  prefer explicit `bound_target_ref`, then an existing stored thread binding,
  over the Teams bot target ref.
- Direct-message human replies remain private and redacted in default status
  snapshots.
- Private outbound `sponsor.ask_question` delivery payload bodies are redacted
  in default status snapshots while remaining available in the raw audit
  records.
- Private `status.reply` and `sponsor.ask_question` safe-output payload text is
  redacted in default status snapshots while raw audit records remain available
  through repository access.
- Private threaded replies without an explicit or existing question/work
  binding create operator attention instead of silently relying on ambiguous
  thread context.

### Tests Run

Commands:

```powershell
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py
pytest -q
```

Results:

```text
9 passed
25 passed
```

Focused coverage added:

- `sponsor.ask_question` creates a safe-output call and sent delivery record
- human question delivery creates a `human_question` thread binding
- human reply in the bound thread creates a `teams_thread` binding to the same
  work/question target
- human reply binding works without fixture-supplied `bound_target_ref`
- private question/reply conversation events and private outbound delivery
  payloads remain redacted in default status output
- private safe-output message/question/reason fields remain redacted in default
  status output
- unbound private threaded replies create connector attention

### Known Limitations

- Real Teams cards, mentions, and Graph/Bot Framework reply handling remain
  later-story scope.
- Authority validation for who may answer the question is Story 10/12 scope.
- Delivery retry and unknown outcome handling remain Story 5 scope.

### Next Engineering Story

Story 5 - Delivery, Retry, Failure Attention, And Idempotency.

Do not begin Story 5 until QA has reviewed Story 4 and any required rework has
passed retest.

## Story 5 - Delivery, Retry, Failure Attention, And Idempotency

Status: engineering implemented; awaiting QA review

### Objective

Make outbound Teams delivery auditable and recoverable so status replies and
sponsor questions cannot falsely claim success when the connector failed,
returned an unknown outcome, or retried.

### Files Changed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_delivery_retry.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Added `delivery_attempts` as first-class audit records linked to delivery
  records.
- Delivery records now support idempotent creation by `idempotency_key`; a
  duplicate outbound send returns the existing delivery without creating a new
  attempt.
- Local Teams delivery now records `sending` transitions and final attempt
  outcomes for `sent`, `failed_transient`, `failed_permanent`, and `unknown`.
- Added explicit retry scheduling via `retry_scheduled`.
- Added `retry_delivery()` for retryable delivery states:
  `failed_transient`, `unknown`, and `retry_scheduled`.
- Sent deliveries are terminal for retry purposes; retrying an already sent
  delivery returns the existing delivery without another human-visible send.
- Permanent failures create non-retryable operator attention.
- Transient failures and unknown outcomes create retryable operator attention,
  with unknown outcomes warning the operator to check Teams before retrying to
  avoid duplicate human-visible messages.
- Default status snapshots now include delivery attempts and delivery status
  counts.
- `status.reply` and `status.complete` validation now rejects agent-authored
  claims that connector delivery succeeded; runtime delivery records own that
  fact.
- Added a connector-path regression using a failing local adapter to prove a
  truthful `status.reply` can record a failed delivery while a delivery-success
  claim is rejected before becoming safe-output state.

### Tests Run

Commands:

```powershell
pytest -q tests\test_v2_safe_outputs.py
pytest -q tests\test_v2_teams_connector_delivery_retry.py
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py
pytest -q
```

Results:

```text
3 passed
4 passed
13 passed
30 passed
```

Initial implementation results before QA rework:

```text
3 passed
12 passed
28 passed
```

Focused coverage added:

- duplicate outbound sends reuse the existing delivery record without a new
  attempt
- transient failure creates retryable attention and can retry to sent
- retrying an already sent delivery does not create a duplicate successful send
- permanent failure creates non-retryable operator attention
- unknown outcome remains visible and retryable, with duplicate-send warning
- retry scheduling is represented in delivery status counts
- connector delivery-success claims are rejected from `status.reply` text
- failed connector delivery through `ConnectorSafeOutputService` leaves
  delivery/attention evidence without accepting a false human-delivery claim

### Known Limitations

- This is still the deterministic local Teams adapter; real Microsoft
  Graph/Bot Framework retry policies, rate limits, and unknown outcome
  classification remain real-connector scope.
- Attention item closure and retry action buttons remain dashboard/operator UX
  scope.

### Next Engineering Story

Story 6 - Separate Visible Role Identities.

Do not begin Story 6 until QA has reviewed Story 5 and any required rework has
passed retest.

## Story 6 - Separate Visible Role Identities

Status: engineering implemented; awaiting QA review

### Objective

Allow each configured role to have a distinct Teams-facing identity while
keeping runtime authorization based on stable configured role bindings rather
than display names.

### Files Changed

- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_teams_connector_foundation.py`
- `tests/test_v2_teams_connector_direct_messages.py`
- `tests/test_v2_teams_connector_project_channels.py`
- `tests/test_v2_teams_connector_human_questions.py`
- `tests/test_v2_teams_connector_delivery_retry.py`
- `tests/test_v2_teams_connector_role_identities.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Replaced simple role-to-external-ref config parsing with explicit
  `RoleIdentity` objects containing `external_ref`, `display_name`, `alias`,
  `mention_handle`, `identity_model`, and `enabled`.
- Supported identity models are `separate_bot`, `shared_gateway`, and `hybrid`.
- Role participant records now store presentation metadata separately from
  runtime `role_id`.
- Connector health records expose the configured identity models for status and
  diagnostics.
- Mention routing resolves explicit role IDs or configured external refs,
  aliases, and mention handles; plain display-name text does not grant routing
  authority.
- Disabled role identities create connector attention and no role assignment.
- Direct messages to ambiguous or disabled identities create operator attention
  rather than silently choosing a role.
- Direct messages with any explicit target hint never fall back to an arbitrary
  single enabled role if that hint does not resolve to an enabled configured
  external identity.
- Disabled or unconfigured role identities cannot send outbound delivery; the
  adapter records operator attention and raises before creating a delivery
  record.
- Outbound delivery payloads include the configured presentation identity used
  for the role that sent the message.
- Added participant metadata storage to the v2 SQLite schema, including an
  idempotent column add for existing local databases.

### Tests Run

Commands:

```powershell
pytest -q tests\test_v2_teams_connector_role_identities.py
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py
pytest -q
```

Results:

```text
6 passed
19 passed
36 passed
```

Focused coverage added:

- role identity config exposes display name, alias, mention handle, identity
  model, enabled state, and external app/bot id
- alias/mention refs resolve to stable role IDs
- display-name-only plain text does not route or authorize a role
- disabled role identities create attention and no assignment
- disabled-role DMs and display-name-only DM targets do not fall back to an
  enabled role
- disabled role identities cannot send outbound delivery
- outbound delivery records include the configured role presentation identity
- bad identity models and duplicate external refs are rejected

### Known Limitations

- This story records and uses local adapter identity metadata only; real Teams
  app registrations, Bot Framework app IDs, Entra consent, and tenant install
  validation remain real-connector/security-release scope.
- Emergency disablement is represented by `enabled=false`; runtime hot-reload
  and operator UI controls remain later operational slices.

### Next Engineering Story

Story 7 - Feature, Epic, Incident, And Focused-Work Channels.

Do not begin Story 7 until QA has reviewed Story 6 and any required rework has
passed retest.

## Story 7 - Feature, Epic, Incident, And Focused-Work Channels

Status: engineering implemented; awaiting QA review

### Objective

Allow project teams to bind additional Teams channels to feature, epic,
incident, or focused-work scopes so conversations and role mentions in those
channels carry the right project/work context without using Teams for
agent-to-agent transport.

### Files Changed

- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_focus_channels.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Added `ChannelBinding` config objects with `channel_ref`, `scope_type`,
  `display_name`, `visibility`, optional `work_scope`, and `private`.
- Supported focus channel scope types are `feature`, `epic`, `incident`, and
  `focused_work`.
- Connector health/status now exposes configured channel bindings for dashboard
  and status views.
- Focus-channel messages are captured as project conversation events with
  `channel_scope` metadata.
- Role mentions inside focus channels create the same role assignments and
  thread bindings as default project channel mentions, with the channel scope
  copied into assignment payloads.
- Private channel events require an explicit channel binding; unbound private
  channels create operator attention and do not create role assignments.
- Channel binding config rejects duplicate/default channel refs and unknown
  scope/visibility values.

### Tests Run

Commands:

```powershell
pytest -q tests\test_v2_teams_connector_focus_channels.py
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py
pytest -q
```

Results:

```text
4 passed
23 passed
40 passed
```

Focused coverage added:

- feature-channel context is captured with channel scope metadata
- focus-channel role mentions and threads follow default routing rules
- assignment payloads include focused channel scope and work scope
- explicitly bound private channels can route role mentions
- unbound private channels create attention and no assignment
- invalid channel binding config is rejected

### Known Limitations

- Runtime channel creation is intentionally not implemented; manual channel
  binding is the v2 local/open-source path for this story.
- Real Teams private-channel membership and permission validation remain
  real-connector/security-release scope.
- Dashboard rendering of channel scope beyond JSON/status read models remains a
  later UI slice.

### Next Engineering Story

Story 8 - Team-Wide Relevance Checks.

Do not begin Story 8 until QA has reviewed Story 7 and any required rework has
passed retest.

## Story 8 - Team-Wide Relevance Checks

Status: engineering implemented; awaiting QA review

### Objective

Make `@all-agents` channel requests quiet by default: every enabled role gets a
lightweight relevance assignment, each role records its relevance decision, and
only materially relevant roles or justified exceptions post human-visible
Teams replies.

### Files Changed

- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/safe_outputs.py`
- `tests/test_v2_teams_connector_team_wide_relevance.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

### Implementation Notes

- Team-wide trigger messages now create a `team_wide_prompt` conversation event.
- Each enabled configured role receives a `team_wide_relevance_check`
  assignment; disabled role identities are not assigned.
- Relevance assignments include conversation, source receipt, thread, channel
  scope, and threshold metadata.
- Added `relevance.record` as a common safe-output tool.
- Added `relevance_checks` records to persist score, threshold, decision,
  reason, no-op, exception reason, safe-output ref, and delivery ref.
- Relevance checks are idempotent by conversation event and role.
- `status_snapshot()` exposes relevance checks and count summaries.
- No-op relevance decisions create no delivery records.
- A run that records a no-op relevance decision is blocked from later posting a
  `status.reply` Teams delivery in the same run.
- Material and exception decisions can be paired with `status.reply` delivery
  evidence, while role-to-role follow-up remains a runtime `consult.request`
  with no Teams delivery.

### Tests Run

Commands:

```powershell
pytest -q tests\test_v2_teams_connector_team_wide_relevance.py
pytest -q tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_team_wide_relevance.py
pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py
pytest -q
```

Results:

```text
4 passed
7 passed
27 passed
44 passed
```

Focused coverage added:

- team-wide trigger creates relevance assignments for all enabled roles
- disabled role identities do not receive relevance assignments
- relevance checks record score, threshold, decision, reason, no-op, exception
  reason, safe-output refs, and delivery refs
- no-op decisions remain silent
- no-op decisions are enforced against later same-run Teams replies
- material input can produce a Teams reply with delivery evidence
- below-threshold exception can record a justified exception
- role-to-role follow-up uses `consult.request` and creates no Teams delivery

### Known Limitations

- The actual relevance scoring worker/prompt is not implemented in this story;
  this story provides the runtime contract and persistence surface.
- Threshold selection is currently a local adapter default of `0.6`.
- Real Teams tenant behavior for team-wide messages remains real-connector
  scope.

## Story 9 - Proactive Work Proposals And Conversation Promotion

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Story 9 implements the explicit bridge from Teams conversation into durable
work capture. The connector still treats messaging input as conversational by
default; durable work is created only when a role emits the
`queue.propose_item` safe-output tool.

### Implementation Notes

- Added a first-class `work_proposals` table and status snapshot surface.
- Added `V2Database.get_queue_item()`, `get_conversation_event()`,
  `record_work_proposal()`, and `list_work_proposals()`.
- Strengthened `queue.propose_item` validation so proposals must include:
  title, summary, source ref, rationale, urgency, suggested owner, work type,
  classification, and initiator.
- Made `queue.propose_item` terminal by default for conversation-promotion
  runs.
- `ConnectorSafeOutputService` now converts a valid `queue.propose_item` call
  into a real queued item plus a source-linked work-proposal audit record.
- Private DM promotions infer `private_source_redacted` from the source
  conversation event and persist references/classification instead of raw
  message text.
- `queue.propose_item` rejects raw conversation text fields such as `body`,
  `message`, `raw_text`, and `raw_message`.
- `status.reply` may carry a `queue_item_id` reference only when that queue
  item already exists; unknown queue references are rejected before recording
  the safe-output call.
- Project-channel free text remains context only and creates no queue item,
  work item, or role assignment by inference.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_work_proposals.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_work_proposals.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py tests\test_v2_teams_connector_work_proposals.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
5 passed
8 passed
32 passed
49 passed
```

### QA Rework

QA found two Story 9 guardrail gaps:

- `status.reply` validated `queue_item_id` references but not `work_item_id`
  references.
- `queue.propose_item` with an unknown `source_conversation_event_id` was
  rejected only after the safe-output call had already been persisted.

Rework completed:

- Added `work_item_id` reference validation for `status.reply` before the
  safe-output call is recorded or delivered.
- Added pre-record validation for `queue.propose_item` source conversation
  events so fake source references fail without durable safe-output evidence.
- Added focused regression tests for both findings.

### Known Limitations

- Queue proposal creation is local-runtime only in this story; queue promotion
  policy and lifecycle start remain later runtime stories.
- Real Teams tenant behavior remains outside local adapter coverage.
- The safe-output front door currently returns a call id in-process; CLI/MCP
  wrappers should expose the created queue/proposal refs directly when those
  front doors are implemented.

## Story 10 - Approval And Human-Response Cards

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Story 10 adds local Teams structured response-card support for approval and
human-response requests. It keeps privileged decisions in runtime state:
agents request the response through safe-output, the connector records a bound
request and delivery, and card submissions are normalized and authorized before
the request can move to responded.

### Implementation Notes

- Added `human_response_requests` and `human_response_submissions` runtime
  tables and status snapshot counts/lists.
- Added `human_response.request` as a common terminal safe-output tool for
  non-release human response contracts.
- Routed `release.request_approval` and `human_response.request` through local
  structured Adaptive Card payload delivery when connector conversation fields
  are present.
- Added card request thread binding with `binding_type=human_response`.
- Added card submission handling with response normalization for approve,
  reject, and request-changes responses.
- Enforced configured human authority before accepting privileged responses.
- Rejected unauthorized submissions without mutating the request.
- Rejected stale/superseded card submissions without mutating the recorded
  accepted response.
- Recorded visible card-update attempts after accepted submissions where the
  local Teams adapter supports it.
- Reused delivery failure attention behavior for card delivery and card update
  failures.
- Added pre-record validation that response-card safe-output payloads cannot
  reference unknown work items.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_response_cards.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_response_cards.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py tests\test_v2_teams_connector_work_proposals.py tests\test_v2_teams_connector_response_cards.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
6 passed
9 passed
38 passed
55 passed
```

### QA Rework

QA found that malformed card submissions such as `response_value=maybe` failed
closed but left no rejected submission record and no attention item.

Rework completed:

- Invalid card submissions now record a `rejected_invalid`
  `human_response_submission` with normalized value `invalid`.
- Invalid card submissions create a retryable `invalid_card_submission`
  connector attention item.
- Added a focused regression proving invalid submissions do not mutate the
  request and do not create update deliveries.

### Known Limitations

- Adaptive Card payloads are local deterministic fixtures; real Bot Framework
  and Teams client rendering/update behavior still require tenant validation.
- Accepted card submissions update local runtime state but do not yet drive the
  broader v2 lifecycle state machine.
- Authority is checked against configured local human authorities; Entra-backed
  identity/authority hardening remains a later story.

## Story 11 - Context Compaction, Retention, And Durable Knowledge

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Story 11 adds local runtime support for source-linked context compaction and
raw retention expiry. Messaging history remains conversational by default; the
context service can compact important source-linked outcomes, while durable
documents, risks, decisions, releases, and role memory still require their own
safe-output or document lifecycle actions.

### Implementation Notes

- Added `focus_channel_days` as a supported connector retention setting.
- Added `conversation_context_summaries` for source-linked compacted summaries.
- Added `retention_expiry_records` for raw expiry tombstones with SHA-256
  content hashes and metadata.
- Added `ContextRetentionService` with:
  - retention-key selection for private DM, project channel, focused channel,
    delivery record, receipt, and compacted summary classes
  - source-linked context compaction from conversation events
  - privacy enforcement preventing private DM context from becoming shared
    context unless an explicit work proposal/promotion exists
  - raw expiry methods for conversation events, external event receipts, and
    delivery records
- Raw expiry scrubs message/card body payloads while preserving source ids,
  metadata, retention class, and content hashes for audit.
- Status snapshots now expose context summary and retention expiry counts and
  records.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_context_retention.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_context_retention.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py tests\test_v2_teams_connector_work_proposals.py tests\test_v2_teams_connector_response_cards.py tests\test_v2_teams_connector_context_retention.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
5 passed
8 passed
43 passed
60 passed
```

### QA Rework

QA found three Story 11 guardrail gaps:

- private compacted summaries were exposed directly in default status
  snapshots
- durable decision summaries could be recorded without a durable reference or
  target
- repeated raw-expiry calls could recompute from already-scrubbed content and
  diverge from the immutable retention expiry hash

Rework completed:

- Default status snapshots now redact private context summary text.
- Durable context classifications such as decision, requirement, risk,
  approval, blocker, instruction, and release fact require either
  `durable_refs` or `target_ref`.
- Retention expiry methods are idempotent by `(source_table, source_id)` and
  return the existing expiry record without mutating already-expired source
  rows.
- Added focused regression coverage for all three findings.

### Known Limitations

- Retention expiry is explicit service invocation in this story; no scheduler
  or age-based automatic expiry loop is implemented yet.
- Compaction summary text is supplied by the caller; this story does not add a
  summarization worker or model prompt.
- Raw expiry keeps hashes and metadata but does not yet create document-library
  artifacts automatically.

### Next Engineering Story

Story 12 - Permission, Consent, Installation, And Authority Hardening.

Do not begin Story 12 until QA has reviewed Story 11 and any required rework has
passed retest.

## Story 12 - Permission, Consent, Installation, And Authority Hardening

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Story 12 adds deterministic local-adapter security hardening for Teams
permission, consent, installation, binding, authority, and credential-material
guardrails. This remains local/runtime validation, but the model is designed so
real Teams/Bot Framework/Graph checks can replace local status inputs later.

### Implementation Notes

- Added `agentic_mesh_v2.permissions` with:
  - startup capability validation for app installation, tenant consent, team
    binding, channel binding, role identity binding, member metadata access,
    and send capability
  - runtime capability checks for receive, send, card send, and card response
    submission paths
  - setup/runtime permission declarations with consent type, required flag,
    broad Graph classification, approval reference, and rationale
  - inline credential-material rejection for secret-like config keys
- Added `connector_permission_checks` for auditable validation evidence.
- Connector startup now records permission checks, marks connector status
  `permission_failed` when validation fails, and creates actionable connector
  attention items.
- Receive, send, card send, and card-response submission fail closed when a
  required runtime capability is missing or revoked.
- Human response authority now resolves from stored connector participant
  records, with optional `people` and `authority_groups` config support. Display
  names and free-text mentions do not authorize privileged actions.
- Broad Graph permissions such as `ChannelMessage.Read.All` require explicit
  documented approval before startup health can pass.
- Status snapshots expose connector permission-check counts and records.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_permissions.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py tests\test_v2_teams_connector_work_proposals.py tests\test_v2_teams_connector_response_cards.py tests\test_v2_teams_connector_context_retention.py tests\test_v2_teams_connector_permissions.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
6 passed
52 passed
66 passed
```

### QA Rework

QA found that an unbound non-default Teams channel could still be accepted as
project-visible context with `channel_scope=None`.

Rework completed:

- Unbound non-DM channels now fail closed before receipt, conversation event,
  assignment, or project-context capture.
- The runtime records a failed `channel_binding:{conversation_ref}` permission
  check and creates actionable `connector_permission_failed` attention.
- Existing private-channel behavior was tightened to the same fail-closed model:
  explicitly bound private channels work; unbound private channels are rejected
  before context capture.
- Added focused regression coverage proving outside-channel raw body text does
  not appear in status snapshots after rejection.

### Known Limitations

- Permission values are deterministic local-adapter inputs; no real tenant,
  Graph, Teams app installation, Bot Framework, Entra group, or resource-specific
  consent validation has been executed.
- Setup/admin permission documentation is represented in structured permission
  declaration fields and test coverage, but the install guide/evidence package
  for a real tenant remains later release work.
- Authority records are local configured people/groups, not live Entra group
  expansion.

## Story 13 - Dashboard And Observability Completion

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Story 13 expands the v2 status dashboard/read model so sponsors and operators
can see connector health, role identities, channel bindings, conversations,
deliveries, relevance checks, context summaries, work proposals, permission
checks, and attention items without reading raw SQLite tables.

### Implementation Notes

- Extended status JSON with `connector_metrics`:
  - inbound events
  - duplicates suppressed
  - active conversations
  - delivery failures
  - relevance decision counts
  - ambiguous binding count
  - private DM promotions
  - compaction count
  - permission failure count
- Added duplicate-suppression audit events when repeated external receipts are
  detected.
- Expanded `/status` HTML with compact sections for:
  - connector metrics
  - role identities
  - channel bindings
  - conversations
  - conversation events
  - permission checks
  - relevance checks
  - work proposals
  - human responses
  - context summaries
- Preserved default redaction behavior for private DM body previews, private
  response-card title/question text, private delivery payloads, private
  safe-output payloads, and private context summaries.
- Added OpenTelemetry spans for connector install, permission validation,
  identity mapping, receive, idempotency, route classification, conversation
  append, role wake, delivery, delivery retry, response binding, relevance,
  compaction, and raw retention expiry.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_permissions.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_context_retention.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_status_dashboard.py tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py tests\test_v2_teams_connector_work_proposals.py tests\test_v2_teams_connector_response_cards.py tests\test_v2_teams_connector_context_retention.py tests\test_v2_teams_connector_permissions.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
3 passed
15 passed
54 passed
69 passed
```

### QA Rework

QA found that bound private-channel messages were stored with project
visibility, so their body previews and raw receipt payloads could appear in
default status JSON and the new HTML conversation-event table.

Rework completed:

- Bound private channels now create conversation events with
  `visibility_scope=private`.
- Default status redaction now treats external receipts from both DMs and
  private channels as private payloads.
- Added a focused dashboard regression proving a private-channel sentinel is
  absent from both status JSON and HTML while the event still records its
  private channel scope.

### Known Limitations

- The HTML view is still a compact operator dashboard, not the final
  authorization-aware control-plane UI.
- Metrics are runtime read-model counts, not a Prometheus/OpenTelemetry metrics
  exporter yet.
- OTEL span tests are smoke-level through code paths; no live collector export
  validation is included in this story.

### Next Engineering Story

Story 14 - Release, Deployment, Rollback, And Dogfood Validation.

Do not begin Story 14 until QA has reviewed Story 13 and any required rework has
passed retest.

## Story 14 - Release, Deployment, Rollback, And Dogfood Validation

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Story 14 adds the local release/deployment validation spine for the v2 Teams
connector. Release Manager can now register a project-scoped Compose deployment
target, validate that the service runs from a project boundary, record a release
deployment with smoke evidence, link required cross-discipline release evidence,
and disable the target for rollback without deleting runtime audit state.

### Implementation Notes

- Added deployment target, deployment run, and release evidence link tables to
  the v2 runtime schema.
- Added repository methods for deployment target registration, disablement,
  deployment-run recording, release-evidence recording, and status read-model
  listing.
- Added `ReleaseService.register_compose_target()` for Compose-backed connector
  release targets.
- Compose validation checks that:
  - at least one Compose file is supplied
  - the named service exists
  - the service command starts the v2 runtime/CLI rather than an unrelated
    process
  - the service has an explicit project boundary through
    `AGENTIC_MESH_PROJECT_FILE` or a `/mesh/project` mount
- Added `ReleaseService.deploy_compose_release()` to execute the configured
  Compose command through an injectable runner before recording deployment.
- Added `ReleaseService.record_compose_deployment()` to record deployment only
  when all smoke checks pass and required evidence links are present for
  product, architecture, security, prompt, engineering, QA, and release.
- Failed Compose command execution records a failed deployment run with command
  output and leaves the work item in release review rather than creating a
  deployed release record.
- Release closure now rejects deployed releases that do not have both a
  successful deployment run and the complete required release evidence links.
- Release deployment now requires a non-empty rollback plan.
- Added `ReleaseService.disable_deployment_target()` so rollback can disable
  the connector target while preserving connectors, conversations, deliveries,
  receipts, permission checks, deployment runs, releases, and events.
- Expanded status snapshots and `/status` HTML with deployment targets,
  deployment runs, and release evidence links.
- Added `docs/operations/v2-teams-connector-release-profile.md` as the dogfood
  release profile covering app registration, consent, installation, bindings,
  credential URL, retention, smoke checks, rollback, and release evidence.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_release_deployment.py tests\test_v2_end_to_end.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
9 passed
73 passed
```

Focused coverage added:

- Compose deployment target registration validates service, command, and
  project boundary.
- Release deployment records a release, deployment run, release evidence links,
  smoke evidence, rollback plan, and closes the work item as released.
- Missing release evidence or failing smoke checks reject deployment claims
  before recording a release.
- Rollback disablement preserves connector runtime state and audit records.
- Disabled deployment targets cannot be released again.
- Status JSON and HTML expose deployment target, deployment run, and evidence
  link data.

### QA Rework

QA found that the older lower-level `record_deployment()` API could still
create a `deployed` release row, and `close_released_work()` would then close
the item without deployment-run rows or complete release evidence links.

Rework completed:

- `close_released_work()` now allows deployed closure only when the release has
  a successful deployment run and complete required evidence links.
- Explicit `no_deployment_disposition` remains closable for honest
  documentation/demo/no-activation outcomes.
- Added regression coverage proving direct `record_deployment()` cannot close a
  work item without deployment-run evidence.
- Updated the end-to-end slice to use `deploy_compose_release()` with complete
  release evidence links.
- Updated the CLI demo slice to close through a no-deployment disposition
  instead of claiming a deployment.
- Added failed Compose-command coverage proving a failed deployment run is
  recorded without creating a release or closing the work.

Retest commands:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_release.py tests\test_v2_release_deployment.py tests\test_v2_cli_server.py tests\test_v2_end_to_end.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Retest results:

```text
15 passed
75 passed
```

### Known Limitations

- This is local Compose/profile validation. A real Microsoft Teams tenant smoke
  package is still required before declaring an enterprise Teams release.
- The release service records deployment evidence but does not run Docker
  Compose commands itself yet; command execution belongs to the configured
  deployment adapter/operator path.
- Release execution is currently proven through an injectable command runner in
  automated tests; real Docker/Teams tenant execution remains release-operator
  evidence for the target environment.
- Release evidence links are structured runtime records; richer document
  library publication and artifact rendering remain follow-on control-plane UI
  work.

### Next Step

QA should review Story 14 against the full Teams connector release gate. If QA
accepts, the v2 Teams connector local MVP has completed Stories 1 through 14
and can move to a release/no-release decision that explicitly records real
tenant smoke status and any residual risks.

## Review Log

- RL-001 | engineering | implementation | Story 1 | Implemented connector
  foundation and local test adapter with focused regression tests. | QA passed
  2026-06-12
- RL-002 | engineering | implementation | Story 2 | Implemented direct role
  message assignment and `status.reply` delivery through the local connector
  adapter. | awaiting QA review 2026-06-12
- RL-003 | engineering | QA rework | Story 2 | Redacted private direct-message
  receipt payload bodies from default status snapshots after QA found the
  partial-redaction gap. | awaiting QA retest 2026-06-12
- RL-004 | engineering | implementation | Story 3 | Implemented project-channel
  context capture and configured role-mention assignments with focused tests. |
  awaiting QA review 2026-06-12
- RL-005 | engineering | implementation | Story 4 | Implemented
  agent-initiated human questions and bound Teams thread replies through the
  local connector adapter. | awaiting QA review 2026-06-12
- RL-006 | engineering | QA rework | Story 4 | Fixed same-thread reply binding
  to use stored human-question thread context, redacted private outbound
  question payloads from default status, and added attention for unbound
  private threaded replies. | awaiting QA retest 2026-06-12
- RL-007 | engineering | QA rework | Story 4 | Redacted private
  `status.reply` and `sponsor.ask_question` safe-output payload text from
  default status snapshots after QA found the remaining privacy surface. |
  awaiting QA retest 2026-06-12
- RL-008 | engineering | implementation | Story 5 | Implemented delivery
  attempt audit records, idempotent outbound send suppression, retry scheduling,
  retryable transient/unknown attention, permanent-failure attention, and
  delivery retry tests. | awaiting QA review 2026-06-12
- RL-009 | engineering | QA rework | Story 5 | Added safe-output validation
  that rejects connector delivery-success claims in status messages, plus a
  connector-path failing-delivery regression test. | awaiting QA retest
  2026-06-12
- RL-010 | engineering | implementation | Story 6 | Implemented explicit
  Teams-facing role identity objects, participant presentation metadata,
  configured alias/mention resolution, disabled identity attention, and outbound
  delivery identity evidence. | awaiting QA review 2026-06-12
- RL-011 | engineering | QA rework | Story 6 | Removed unsafe direct-message
  fallback when target hints do not resolve to enabled configured identities and
  blocked outbound delivery from disabled role identities. | awaiting QA retest
  2026-06-12
- RL-012 | engineering | implementation | Story 7 | Implemented focused channel
  bindings, scoped conversation/assignment metadata, private-channel explicit
  binding enforcement, and focus-channel regression tests. | awaiting QA review
  2026-06-12
- RL-013 | engineering | implementation | Story 8 | Implemented team-wide
  relevance assignments, `relevance.record`, relevance-check persistence, quiet
  no-op behavior, and runtime consult/no-Teams follow-up regression tests. |
  awaiting QA review 2026-06-12
- RL-014 | engineering | QA hardening | Story 8 | Added enforcement that a
  same-run no-op relevance decision cannot later post a Teams `status.reply`,
  closing the residual channel-noise gap found by QA. | awaiting QA retest
  2026-06-12
- RL-015 | engineering | implementation | Story 9 | Implemented explicit
  conversation-to-queue proposal capture through `queue.propose_item`, work
  proposal audit records, private DM redaction, no-inference regression tests,
  and queue-reference validation for `status.reply`. | awaiting QA review
  2026-06-12
- RL-016 | engineering | QA rework | Story 9 | Added `status.reply`
  work-item reference validation and moved `queue.propose_item` source-event
  validation ahead of safe-output persistence, with focused regression tests. |
  awaiting QA retest 2026-06-12
- RL-017 | engineering | implementation | Story 10 | Implemented
  human-response request/submission persistence, structured approval card
  delivery for `release.request_approval` and `human_response.request`,
  authority-checked submissions, stale-card rejection, card-update delivery
  evidence, and focused tests. | awaiting QA review 2026-06-12
- RL-018 | engineering | QA rework | Story 10 | Added rejected-invalid
  submission audit records and retryable attention for malformed card
  responses, with focused regression coverage. | awaiting QA retest 2026-06-12
- RL-019 | engineering | implementation | Story 11 | Implemented context
  summary persistence, retention expiry tombstones with content hashes,
  configurable focus-channel retention, private-DM compaction boundaries, and
  raw expiry tests for events, receipts, and delivery records. | awaiting QA
  review 2026-06-12
- RL-020 | engineering | QA rework | Story 11 | Redacted private context
  summaries from default status, required durable targets for durable
  classifications, and made raw expiry idempotent so original hashes remain
  authoritative. | awaiting QA retest 2026-06-12
- RL-021 | engineering | implementation | Story 12 | Implemented connector
  permission checks, fail-closed runtime capability enforcement, broad Graph
  approval gating, people/group authority mapping, and inline credential
  guardrails. | awaiting QA review 2026-06-12
- RL-022 | engineering | QA rework | Story 12 | Rejected unbound Teams channel
  ingress before project context capture and added regression coverage for
  outside-boundary channel messages. | awaiting QA retest 2026-06-12
- RL-023 | engineering | implementation | Story 13 | Added connector metrics,
  full connector dashboard sections, duplicate-suppression metrics, private
  redaction smoke tests, and OTEL spans around connector receive/routing,
  delivery, relevance, permission, response, and compaction paths. | awaiting
  QA review 2026-06-12
- RL-024 | engineering | QA rework | Story 13 | Marked bound private-channel
  events as private visibility, redacted private-channel receipt payloads, and
  added status JSON/HTML redaction regression coverage. | awaiting QA retest
  2026-06-12
- RL-025 | engineering | implementation | Story 14 | Implemented
  project-scoped Compose release target validation, Compose command execution,
  deployment-run recording, release evidence links, rollback disablement,
  status visibility, and the dogfood release profile. | awaiting QA review
  2026-06-12
- RL-026 | engineering | QA rework | Story 14 | Closed the false-release
  bypass by requiring deployed release closure to have successful deployment-run
  evidence and complete release evidence links; moved the CLI demo to explicit
  no-deployment closure. | awaiting QA retest 2026-06-12
