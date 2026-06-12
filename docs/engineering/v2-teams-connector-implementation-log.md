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
