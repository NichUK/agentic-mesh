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

### Next Engineering Story

Story 10 - Approval And Human-Response Cards.

Do not begin Story 10 until QA has reviewed Story 9 and any required rework has
passed retest.

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
