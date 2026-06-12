# V2 Teams Connector Story 1 QA Results

Status: QA reviewed

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 1 - Connector Foundation And Local Test Adapter.

Primary references:

- `docs/engineering/v2-teams-connector-implementation-plan.md`
- `docs/engineering/v2-teams-connector-implementation-log.md`
- `docs/qa/v2-teams-connector-test-plan.md`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_teams_connector_foundation.py`

## QA Decision

Story 1 passes as an internal connector-foundation slice.

Engineering rework is not required before starting Story 2. Story 2 must,
however, close the private-DM status redaction gap before it can pass QA or be
treated as releasable beyond foundation work.

The Teams connector is not release-complete. This result covers only the local
foundation slice described for Story 1.

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` and observed existing dirty/untracked Story 1 work. |
| `pytest -q tests/test_v2_teams_connector_foundation.py` | Passed: 3 passed in 0.17s. |
| `pytest -q` | Passed: 19 passed in 0.94s. |
| `agentic-mesh --db .tmp/v2-qa-story1.sqlite3 init-db` | Failed; console script was not available on PATH in this shell. |
| `python -m agentic_mesh_v2.cli --db .tmp/v2-qa-story1.sqlite3 init-db` | Passed; initialized the v2 database. |
| `python -m agentic_mesh_v2.cli --db .tmp/v2-qa-story1.sqlite3 status-json` | Passed; empty status JSON exposed connector foundation counts and arrays. |
| In-memory private DM status probe through `LocalTeamsTestAdapter` | Passed as a probe; confirmed current status snapshot exposes `private private decision text`. This is a privacy gap for Story 2, not a Story 1 foundation blocker. |

## Acceptance Assessment

| Story 1 expectation | QA result |
| --- | --- |
| Connector-neutral records for connectors, participants, conversations, events, receipts, deliveries, thread bindings, and attention items | Pass. Implemented through v2 database tables and repository methods. |
| Additive schema creates foundation tables | Pass. `migrate()` creates the Story 1 connector tables without removing existing v2 runtime tables. |
| Config fixture validates project, default channel, role aliases, sponsor/operator mappings, retention, and external base URL | Pass. Tests cover required-field rejection and unknown retention-key rejection. |
| Local Teams-style adapter replays DM, channel, role mention, duplicate, thread, and send-failure events | Pass. Focused tests cover these paths, including duplicate receipt reuse, thread binding, unknown-role attention, and failed delivery attention. |
| Status JSON exposes connector health and foundation counts | Pass. Empty and populated snapshots expose foundation fields and counts. |
| Teams remains human collaboration surface, not agent-to-agent transport | Pass for Story 1. The adapter records connector events and delivery state only; it does not create role-to-role handoffs or mutate durable work from message text. |

## Coverage

Automated coverage is sufficient for Story 1 foundation gating:

- migration/read-model smoke through `V2Database.migrate()` and status snapshot
- connector config validation
- connector and participant installation
- private DM event capture
- duplicate inbound receipt suppression
- unmentioned project-channel context capture
- configured role mention with thread binding
- unknown role mention attention item
- failed outbound delivery record and attention item
- empty connector status snapshot

The full test suite remains green after the database and status changes.

## Gaps And Risks

- Private DM previews are currently exposed in default `status_snapshot()` data through `conversation_events[*].body_preview`. This is explicitly a Story 2 release gate in the QA plan, and it must be fixed before Story 2 passes.
- Story 1 tests do not directly assert the physical table list with `PRAGMA table_info`; they validate tables through repository behavior and status output. This is acceptable for foundation, but stronger schema contract tests should grow as the connector schema stabilizes.
- Delivery idempotency beyond the first outbound record is not complete. Duplicate safe-output delivery, retry, unknown outcome, and permanent failure behavior belong to Story 5.
- No real Microsoft Teams, Bot Framework, Graph, Entra, permission, consent, installation, or tenant smoke evidence exists. These are later-story and final-release gates.
- Role assignment creation, `status.reply` integration, private DM answer flow, no-work-by-default evidence, and default private-body redaction remain Story 2 scope.

## Rework Decision

No Story 1 rework is required before Engineering starts Story 2.

Required Story 2 QA gates:

- redact private DM body previews from default status JSON and HTML views
- prove ordinary DMs create no queue item or work item by inference
- route role replies through `status.reply` with exactly one delivery record
- preserve duplicate inbound idempotency without duplicate replies or assignments

## Review Log

- RL-001 | qa-engineer | Story 1 QA | Connector foundation passes as an internal slice. No blocking rework before Story 2. Private DM status redaction is carried as a required Story 2 gate. | accepted 2026-06-12

# V2 Teams Connector Story 2 QA Results

Status: QA reviewed - rework required

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 2 - Role Direct Messages And `status.reply`.

Primary references:

- `docs/engineering/v2-teams-connector-implementation-plan.md`
- `docs/engineering/v2-teams-connector-implementation-log.md`
- `docs/qa/v2-teams-connector-test-plan.md`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/safe_outputs.py`
- `tests/test_v2_teams_connector_foundation.py`
- `tests/test_v2_teams_connector_direct_messages.py`

## QA Decision

Story 2 does not pass the QA release gate yet.

Focused and full automated tests pass, and the implemented path proves direct
role DMs can create one private role assignment without queue/work inference
and can route a role `status.reply` into one sent delivery record.

Engineering rework is required before Story 3 because private DM status
redaction is only partially fixed. `conversation_events[*].body_preview` is
redacted, but default `status_snapshot()` still exposes the raw private DM body
through `external_event_receipts[*].payload.body`.

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` and observed existing dirty/untracked Story 2 work. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py` | Passed: 4 passed in 0.25s. |
| `pytest -q` | Passed: 20 passed in 1.00s. |
| In-memory private DM status redaction probe through `LocalTeamsTestAdapter` and `status_snapshot()` | Failed Story 2 privacy expectation: `conversation_events[*].body_preview` was redacted, but `external_event_receipts[*].payload.body` still contained `private roadmap question qa leak probe`; `leaks_secret_in_snapshot=True`. |

## Acceptance Assessment

| Story 2 expectation | QA result |
| --- | --- |
| Direct message to configured role creates a private role assignment | Pass. The local adapter creates one `direct_conversation` role assignment for the addressed role. |
| Duplicate inbound DM does not duplicate side effects | Pass for assignment creation. Duplicate replay reuses the original receipt and does not create a second role assignment. |
| Ordinary DM creates no queue item or work item by inference | Pass. Focused test and snapshot evidence show zero queue items and zero work items after the ordinary DM path. |
| Role reply flows through `status.reply` and creates one outbound delivery record | Pass for the implemented local adapter path. `ConnectorSafeOutputService` records the safe-output call and creates one sent delivery record with purpose `status.reply`. |
| Complete Markdown reply is sent without truncation | Partially covered. The local adapter stores the full supplied message body in the delivery payload and no truncation path is present, but the test does not directly assert a long Markdown or formatting-preservation case. |
| Private DM body text is redacted from default status views | Fail. `conversation_events[*].body_preview` is redacted, but raw receipt payloads in `external_event_receipts` still expose the private DM body in default `status_snapshot()` output. |

## Coverage

Automated coverage is good for the functional happy path:

- role-targeted direct message replay
- duplicate inbound DM replay
- no queue item or work item from ordinary DM text
- role assignment status visibility
- safe-output recording for `status.reply`
- one sent delivery record for the local adapter reply path
- default conversation-event preview redaction

The coverage gap is specifically around whole-snapshot privacy. The Story 2
test checks only `conversation_events[*].body_preview`, not every default
status field that can carry the raw private message text.

## Private DM Redaction Assessment

Private DM status redaction is not fixed.

The implemented redaction masks the conversation event preview, but the default
status snapshot still returns raw private message text through
`external_event_receipts[*].payload.body`. Because `status-json` and the status
server use this snapshot as an operator-facing read model, this does not meet
the Story 2 QA gate: default status views must not leak private body text.

Recommended minimum rework:

- redact or omit private DM bodies from `external_event_receipts` in
  `status_snapshot()`
- add a regression test that searches the entire default snapshot for a private
  sentinel string, not only `conversation_events[*].body_preview`
- consider whether outbound private DM delivery payload bodies should also be
  redacted or separated into an authorization-aware view before Story 13

## Gaps And Risks

- `status.reply` duplicate outbound suppression remains later Story 5 scope,
  but Story 2 should avoid claiming more than the single-run local adapter
  behavior currently tested.
- Role assignment claiming and live role-service DM loops are still manual or
  test-driven.
- No real Teams, Bot Framework, Graph, Entra, permission, consent, installation,
  tenant smoke, or real identity-model evidence exists; these remain later
  gates.
- Long Markdown formatting and split-message behavior are not exercised yet.
- The `status.reply` conversation-binding payload contract remains provisional;
  this is already tracked as a QA plan gap for later safe-output stories.

## Rework Decision

Engineering rework is required before starting Story 3.

Required retest scope:

- focused Story 2 tests
- full `pytest -q`
- whole-snapshot private sentinel regression proving no default status field
  exposes private DM body text

## Review Log

- RL-002 | qa-engineer | Story 2 QA | Functional Story 2 tests pass, but the
  story fails the privacy release gate because default status snapshots still
  expose private DM body text through raw external event receipt payloads. |
  rework required 2026-06-12

# V2 Teams Connector Story 2 QA Re-Test

Status: QA re-tested - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Re-Test Scope

Re-reviewed Story 2 after Engineering rework for the previously failed private
DM default-status redaction gate.

Primary references:

- `docs/engineering/v2-teams-connector-implementation-log.md`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_teams_connector_direct_messages.py`
- `tests/test_v2_teams_connector_foundation.py`

## Re-Test Decision

Story 2 now passes QA for the implemented local Teams connector scope.

Engineering rework covers the prior privacy failure: default
`status_snapshot()` output now redacts private direct-message body text from
both `conversation_events` and `external_event_receipts`. Story 2 does not need
additional rework before Story 3.

This is not a final Teams connector release approval. Real Teams tenant
integration, delivery retries, duplicate outbound suppression, authorization
aware debug views, long Markdown reply coverage, and live role assignment loops
remain later-story scope.

## Commands Run

| Command | Result |
| --- | --- |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py` | Passed: 4 passed in 0.25s. |
| `pytest -q` | Passed: 20 passed in 0.99s. |
| In-memory private DM whole-snapshot sentinel probe through `LocalTeamsTestAdapter`, `RoleService`, `ConnectorSafeOutputService`, and `status_snapshot()` | Passed on rerun with explicit database close. `before_snapshot_contains_sentinel=false`, `after_snapshot_contains_sentinel=false`, `conversation_event_body_preview=[redacted private conversation]`, `conversation_event_payload_contains_body=false`, `external_receipt_payload_body=[redacted private conversation]`, `external_receipt_body_redacted=true`, `raw_database_receipt_body_preserved=true`, `delivery_records=1`, `delivery_statuses={"sent": 1}`, `queue_items=0`, and `work_items=0`. An initial version of this probe printed the same passing privacy result but exited nonzero because the temporary SQLite file was still open during Windows cleanup; it was rerun cleanly. |

## Redaction Assessment

Private DM redaction now covers the previously failed default status read-model
paths:

- `conversation_events[*].body_preview` is redacted to
  `[redacted private conversation]` for private direct-message events.
- `conversation_events[*].payload` does not expose the private message body in
  the tested local adapter path.
- `external_event_receipts[*].payload.body` is redacted to
  `[redacted private conversation]` and marks `body_redacted=true` for DM
  payloads.
- Raw database storage still preserves the original receipt payload body for
  future authorized/debug paths; the default status snapshot is the redacted
  operator-facing view.

## Acceptance Re-Test

| Story 2 expectation | Re-test result |
| --- | --- |
| Direct message to configured role creates a private role assignment | Pass. Existing focused test remains green. |
| Duplicate inbound DM does not duplicate side effects | Pass. Existing focused test remains green. |
| Ordinary DM creates no queue item or work item by inference | Pass. Focused test and whole-snapshot probe both show `queue_items=0` and `work_items=0`. |
| Role reply flows through `status.reply` and creates one outbound delivery record | Pass. Focused test and probe both show one sent delivery record for the local adapter path. |
| Private DM body text is redacted from default status views | Pass. Whole-snapshot sentinel probe found no private sentinel before or after the reply, and verified both conversation event and external receipt redaction. |

## Review Log

- RL-003 | qa-engineer | Story 2 QA re-test | Engineering rework closes the
  private DM default-status leak for both conversation events and external
  event receipts. Story 2 passes for local connector scope and may proceed to
  Story 3. | accepted 2026-06-12

# V2 Teams Connector Story 3 QA Results

Status: QA reviewed - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 3 - Project Channel Capture And Role Mentions.

Primary references:

- `docs/engineering/v2-teams-connector-implementation-plan.md`
- `docs/engineering/v2-teams-connector-implementation-log.md`
- `docs/qa/v2-teams-connector-test-plan.md`
- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_foundation.py`
- `tests/test_v2_teams_connector_direct_messages.py`
- `tests/test_v2_teams_connector_project_channels.py`

## QA Decision

Story 3 passes QA for the implemented local Teams connector scope.

Engineering rework is not required before Story 4. The implemented local
adapter stores unmentioned project-channel messages as shared project context
without role assignment or delivery noise, creates focused assignments only for
configured mentioned roles, suppresses duplicate inbound mention side effects,
and records operator attention for unknown role mentions without guessing a
route.

This is not a final Teams connector release approval. Real Teams mention entity
parsing, focus channels, team-wide relevance, real tenant permissions,
outbound retry/idempotency, and live role assignment claiming remain later
story scope.

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` and observed existing uncommitted Engineering changes in `docs/engineering/v2-teams-connector-implementation-log.md`, `src/agentic_mesh_v2/connectors.py`, and untracked `tests/test_v2_teams_connector_project_channels.py`. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py` | Passed: 7 passed in 0.46s. |
| `pytest -q` | Passed: 23 passed in 1.19s. |
| In-memory Story 3 status probe through `LocalTeamsTestAdapter` and `status_snapshot()` | Initial run printed expected counts but failed Windows temp cleanup because the SQLite connection was still open. Rerun with explicit `db.close()` passed and produced `queue_items=0`, `work_items=0`, `role_assignments=2`, `delivery_records=0`, `external_event_receipts=3`, `conversation_events=3`, `thread_bindings=1`, `attention_items=1`, `assignment_roles=["product-manager", "qa-engineer"]`, and `attention_reasons=["unknown_role_mention"]`. |

## Acceptance Assessment

| Story 3 expectation | QA result |
| --- | --- |
| Unmentioned configured project-channel messages are stored as shared context without waking every role | Pass. Focused test stores a project-visible conversation event and creates zero role assignments. Additional probe also showed zero queue items, zero work items, and zero delivery records for unmentioned channel context. |
| Role mentions resolve only through configured aliases or identities | Pass for the local normalized fixture path. The adapter accepts explicit `mentioned_roles`, validates each role id against configured `role_identities`, and does not create assignments for unknown roles. |
| Mentioned role receives a focused assignment bound to the source thread | Pass. Mentioned-role assignments use `assignment_type=channel_role_mention`, `visibility_scope=project`, source receipt/conversation/message payload refs, and the tested thread creates one thread binding. |
| Multiple configured role mentions create explicit assignments for each role | Pass. Product Manager and QA Engineer mentions create two focused assignments with no extra role assignments. |
| Duplicate role-mention events suppress duplicate side effects | Pass. Duplicate replay reuses the original receipt and returns before creating additional conversation events, thread bindings, or role assignments. |
| Unknown mentions create attention items and do not guess a route | Pass. Unknown role mention creates one `unknown_role_mention` connector attention item and zero role assignments. |
| No low-value acknowledgement is posted for unmentioned context | Pass for the local adapter path. No delivery record is created for unmentioned project-channel context. |

## Coverage Assessment

Story 3 automated coverage is sufficient for this local connector slice:

- project-channel no-wake behavior
- shared project context visibility and visible project body preview
- configured multi-role mention assignment
- source thread binding for mentioned-role channel threads
- duplicate inbound mention suppression
- unknown mention attention without guessed assignment
- regression protection across the prior Story 1 foundation and Story 2 direct
  message/status-reply paths

The full test suite remains green after the Story 3 connector changes.

## Gaps And Risks

- The local adapter still relies on explicit `mentioned_roles` fixture input
  rather than parsing real Teams mention entities or display handles from raw
  Teams payloads. This is acceptable for Story 3 local-slice QA and remains a
  real Teams adapter/substrate risk for later stories.
- Mixed known and unknown mentions in the same message are not covered by the
  current focused tests. The current adapter treats any unknown role in the
  mention list as an `unknown_role_mention` route and creates no assignments.
  Engineering should clarify the desired behavior before real Teams mention
  parsing ships.
- Story 3 does not cover feature, epic, incident, or focused-work channels;
  those remain Story 7 scope.
- Story 3 does not cover team-wide relevance checks or material/no-op response
  scoring; those remain Story 8 scope.
- Role assignment claiming remains a live role-service/runtime scope item, not
  completed by the local connector adapter tests.
- No real Microsoft Teams, Bot Framework, Graph, Entra, consent, installation,
  permission, tenant, or role identity deployment evidence exists yet.

## Rework Decision

No Engineering rework is required before starting Story 4.

Recommended Story 4 QA gates:

- every `sponsor.ask_question` delivery has an originating runtime binding
- replies bind back to the correct question, work item, approval, risk, or
  document context
- human loop-in records participants and route changes
- ambiguous replies create connector attention and do not mutate work state
- safe-output remains the only path for durable work, approval, risk, document,
  release, or handoff changes

## Review Log

- RL-004 | qa-engineer | Story 3 QA | Project-channel context capture and
  configured role-mention routing pass for the local connector scope. No
  blocking Engineering rework is required before Story 4. | accepted
  2026-06-12

# V2 Teams Connector Story 4 QA Results

Status: QA reviewed - rework required

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 4 - Agent-Initiated Human Questions And Thread Binding.

Primary references:

- `docs/engineering/v2-teams-connector-implementation-plan.md`
- `docs/engineering/v2-teams-connector-implementation-log.md`
- `docs/qa/v2-teams-connector-test-plan.md`
- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_foundation.py`
- `tests/test_v2_teams_connector_direct_messages.py`
- `tests/test_v2_teams_connector_project_channels.py`
- `tests/test_v2_teams_connector_human_questions.py`

## QA Decision

Story 4 does not pass the QA release gate yet.

Focused and full automated tests pass, and the happy path proves
`sponsor.ask_question` can create a safe-output call, create one sent delivery
record, record a `human_question` thread binding, and redact private inbound
question/reply conversation events in the default status snapshot.

Engineering rework is required before Story 5 because reply binding currently
depends on fixture-supplied `bound_target_ref` instead of resolving the
existing `human_question` thread binding. A same-thread sponsor reply without
`bound_target_ref` binds to the Teams bot target, not the originating work or
question target. Default status output also exposes private outbound
question/reason text through `delivery_records[*].payload.body`.

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` and observed existing uncommitted Engineering changes in `docs/engineering/v2-teams-connector-implementation-log.md`, `src/agentic_mesh_v2/connectors.py`, and untracked `tests/test_v2_teams_connector_human_questions.py`. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py` | Passed: 8 passed in 0.53s. |
| `pytest -q` | Passed: 24 passed in 1.29s. |
| In-memory Story 4 binding and privacy probe through `LocalTeamsTestAdapter`, `RoleService`, `ConnectorSafeOutputService`, and `status_snapshot()` | Failed Story 4 release expectations. A reply in `thread-human-question-probe` without `bound_target_ref` created `teams_thread -> bot-product-manager` while the existing `human_question` binding for the same thread targeted `work-teams-connector`. The same default snapshot redacted private inbound source and answer text, but still contained `private question sentinel` and `private reason sentinel` in the delivery record payload. No attention item was created for the ambiguous or mismatched binding. |

## Acceptance Assessment

| Story 4 expectation | QA result |
| --- | --- |
| `sponsor.ask_question` creates a safe-output call and delivery record | Pass for the local adapter happy path. The role run records one `sponsor.ask_question` safe-output call and one sent delivery record with purpose `sponsor.ask_question`. |
| Outbound human question has an originating runtime binding | Partial. `deliver_human_question()` records a `human_question` thread binding to `work_item_id`, `target_ref`, or the safe-output `call_id`, so the outbound question can carry a source binding. The payload contract remains provisional. |
| Human question delivery creates a `human_question` thread binding | Pass on the tested happy path. The binding uses the supplied thread ref and target work/question ref. |
| Sponsor answer in the same Teams thread binds back to the original question/source work | Fail. The implementation records inbound thread replies with `target_ref=event.bound_target_ref or event.target_ref`; it does not look up the existing `human_question` binding by `thread_ref`. The focused test passes only because the fixture supplies `bound_target_ref=work-teams-connector`. |
| Ambiguous or unbound replies create attention and do not mutate work state | Not covered and currently failing for the probed ambiguity. A same-thread reply that cannot infer the original target creates no attention item and binds to the bot target. |
| Private question/reply conversation events remain redacted in default status output | Pass for inbound conversation events and external event receipts. Source and answer DM body sentinels were not present in the default snapshot. |
| Private outbound human-question content is redacted from default status output | Fail. The default snapshot returns delivery records unredacted, including the private question and reason text in `delivery_records[*].payload.body`. |
| Human loop-in records added participants and preserves the originating runtime binding | Not implemented or covered in Story 4 tests. This remains a QA-plan gap before this story can claim the broader human-loop-in scenario. |

## Coverage Assessment

Automated coverage is useful but too narrow for Story 4 release:

- happy-path `sponsor.ask_question` safe-output routing
- one sent delivery record for a direct-message human question
- `human_question` thread binding creation
- fixture-assisted human reply binding using explicit `bound_target_ref`
- private inbound conversation-event preview redaction
- regression coverage across Stories 1, 2, and 3

Missing or insufficient coverage:

- reply binding lookup from existing `human_question` bindings when Teams only
  supplies the thread/message reference
- ambiguous reply behavior and connector attention creation
- same-thread reply without `bound_target_ref`
- human loop-in participant or route-change audit
- channel or group-chat question routes beyond accepting a destination type
- whole-snapshot redaction of private outbound delivery payloads
- negative evidence that human replies do not mutate work, approval, risk,
  document, release, or handoff state from free text

## Private Redaction Assessment

Private inbound body redaction remains intact for Story 4:

- private `conversation_events[*].body_preview` values are redacted
- private direct-message `external_event_receipts[*].payload.body` values are
  redacted
- the probe found no private source or answer sentinels in the default snapshot

Private outbound question redaction is incomplete:

- `delivery_records` are returned unchanged by `status_snapshot()`
- direct-message `sponsor.ask_question` delivery payloads include the full
  question and reason text
- the default snapshot exposed private question and reason sentinels through
  `delivery_records[*].payload.body`

Recommended minimum rework:

- redact private direct-message delivery payload bodies in default
  `status_snapshot()` output or omit the payload from the default operator view
- add a whole-snapshot sentinel regression for private source, answer,
  question, and reason text
- preserve raw database delivery payloads only for a future
  authorization-aware debug path

## Required Engineering Rework

Engineering rework is required before starting Story 5.

Minimum required rework:

- when an inbound reply has `thread_ref`, resolve the existing
  `human_question` binding for that connector/conversation/thread and bind the
  reply to the original target without requiring fixture-supplied
  `bound_target_ref`
- create a connector attention item when a reply thread is ambiguous, missing a
  binding, or would otherwise bind only to a bot target
- add regression tests for same-thread sponsor replies without
  `bound_target_ref`
- redact or omit private direct-message delivery payload bodies from default
  status output and add whole-snapshot sentinel tests

Recommended additional rework or explicit deferral:

- document the `sponsor.ask_question` payload contract for `work_item_id`,
  `target_ref`, `conversation_id`, `destination_ref`, `destination_type`, and
  `thread_ref`
- add a minimal human loop-in audit test or explicitly defer loop-in to a later
  story with sponsor-visible risk
- add negative assertions that sponsor answer ingestion does not create queue
  items, work items, approvals, risks, documents, releases, or handoffs from
  free text

## Gaps And Risks

- Real Teams cards, mentions, Graph/Bot Framework reply payloads, Entra
  identity, consent, installation, tenant permissions, and real thread ids are
  not covered yet.
- Delivery retry, duplicate outbound suppression, transient failure, permanent
  failure, and unknown outcome handling remain Story 5 scope.
- Authority validation for who may answer a question remains Story 10/12 scope.
- The current local adapter can accept `destination_type` values such as
  channel or group chat, but Story 4 does not prove Teams-compatible channel or
  group-chat question routing, human mentions, or broader decision threads.
- The Story 4 test does not protect against the exact binding failure found by
  the probe because it supplies the desired target as fixture input.

## Rework Decision

Engineering rework is required before Story 5.

Story 5 should not begin until Story 4 is re-tested with:

- focused Story 1-4 connector tests
- full `pytest -q`
- a same-thread sponsor-reply test without `bound_target_ref`
- an ambiguous-thread attention test
- a whole-snapshot private outbound question/reason redaction test

## Review Log

- RL-005 | qa-engineer | Story 4 QA | Happy-path agent-initiated human
  questions pass automated tests, but the story fails the release gate because
  reply binding does not resolve the existing human-question thread binding and
  default status exposes private outbound question text in delivery payloads. |
  rework required 2026-06-12

# V2 Teams Connector Story 4 Current Working Tree QA Check

Status: QA checked current tree - rework still required

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 4 only - `sponsor.ask_question` delivery from a role to
a human, question thread binding, human reply binding back to the work/question
context, and private conversation redaction.

Files inspected:

- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_human_questions.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; current tree has modified `docs/engineering/v2-teams-connector-implementation-log.md`, modified `src/agentic_mesh_v2/connectors.py`, and untracked `tests/test_v2_teams_connector_human_questions.py`. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py` | Passed: 8 passed in 0.54s. |
| In-memory Story 4 binding/redaction probe through `LocalTeamsTestAdapter`, `RoleService`, `ConnectorSafeOutputService`, and `status_snapshot()` | Failed Story 4 expectations: same-thread reply without `bound_target_ref` produced `human_question -> work-teams-connector` but `teams_thread -> bot-product-manager`; `connector_attention_items=0`; private inbound source/answer text was redacted, but private outbound question/reason text remained visible in `delivery_records[*].payload.body`. |

## Coverage Assessment

The new focused test covers the happy path for a role using
`sponsor.ask_question`, a sent delivery record, a `human_question` thread
binding, fixture-assisted human reply binding, and private inbound
conversation-event redaction.

Coverage is not yet sufficient for Story 4 because it does not prove reply
binding from the stored thread binding itself, ambiguous-thread attention,
human loop-in participant audit, channel/group-chat question routes, or
whole-snapshot redaction of private outbound question content.

## Gaps

- Human replies depend on fixture-supplied `bound_target_ref`; the adapter does
  not resolve the existing `human_question` binding from `thread_ref`.
- Ambiguous or unbound replies do not create connector attention items.
- Default status snapshots still expose private direct-message outbound
  question and reason text through delivery record payloads.
- Human loop-in recording is not implemented or covered in the Story 4 test.

## Rework Decision

Rework is required before Story 5.

Minimum retest should include focused Story 1-4 connector tests, a same-thread
reply test without `bound_target_ref`, an ambiguous-thread attention test, and
a whole-snapshot sentinel test proving private source, answer, question, and
reason text are not exposed by default status output.

## Review Log

- RL-006 | qa-engineer | Story 4 current-tree QA | Focused tests pass, but
  current-tree inspection and probe confirm Story 4 still needs rework for
  stored thread-binding resolution, ambiguous reply attention, and private
  outbound delivery redaction before Story 5. | rework required 2026-06-12

# V2 Teams Connector Story 4 Rework QA Retest

Status: QA retested current tree - requested Story 4 rework mechanics pass,
with one default-status privacy gap still open

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story 4 rework only:

- `sponsor.ask_question` delivery from a role to a human
- stored `human_question` thread binding resolution when a sponsor reply lacks
  `bound_target_ref`
- connector attention for unbound private threaded replies
- redaction of private outbound direct-message delivery payloads in default
  status output

Files inspected:

- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_teams_connector_human_questions.py`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed current branch `codex/v2-runtime-reset` with existing uncommitted Story 4 changes and no source/test edits by QA. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py` | Passed: 9 passed in 0.59s. |
| In-memory Story 4 binding/attention/redaction probe through `LocalTeamsTestAdapter`, `RoleService`, `ConnectorSafeOutputService`, and `status_snapshot()` | Passed for requested mechanics: same-thread reply without `bound_target_ref` resolved `teams_thread -> work-teams-connector`, unbound private thread created one `unbound_thread_reply` attention item, and `delivery_records[*].payload.body` was `[redacted private conversation]` with no private question/reason sentinel in delivery records. |

## Retest Result

Pass for the explicit rework mechanics:

- `sponsor.ask_question` creates the safe-output call, sent delivery record,
  and `human_question` thread binding.
- sponsor replies in the same thread no longer require fixture-supplied
  `bound_target_ref`; the adapter resolves the stored `human_question` binding.
- unbound private threaded replies create connector attention instead of being
  silently trusted.
- private direct-message outbound delivery payload bodies are redacted from
  `delivery_records` in `status_snapshot()`.

## Residual Gaps

- The default status snapshot still exposes private `sponsor.ask_question`
  question and reason text through `safe_output_calls[*].payload`. Delivery
  payload redaction is fixed, but whole-snapshot private outbound redaction is
  not complete unless `safe_output_calls` is intentionally outside the default
  privacy contract.
- Story 4 remains local-adapter coverage only. Real Teams cards, Graph/Bot
  Framework thread ids, mentions, tenant permissions, and channel/group-chat
  delivery semantics are not exercised.
- Human loop-in participant audit and authority validation for who may answer a
  question remain uncovered or deferred.

## Story 5 Gate

Story 5 should not begin yet if the Story 4 privacy gate means no private
question/reason text appears anywhere in default `status-json` output.

If the sponsor explicitly accepts `safe_output_calls` as an internal,
unredacted status surface, then Story 5 may begin for the four requested rework
mechanics tested here.

## Review Log

- RL-007 | qa-engineer | Story 4 rework retest | Focused connector tests and
  probe pass for `sponsor.ask_question` delivery, stored thread binding
  resolution without `bound_target_ref`, unbound private thread attention, and
  delivery-record redaction. Default status still exposes private question and
  reason text via `safe_output_calls`, so Story 5 should wait unless that
  status surface is explicitly accepted as out of scope. | conditional pass
  2026-06-12

# V2 Teams Connector Story 4 Final Privacy QA Retest

Status: QA retested current tree - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Final Story 4 privacy retest for the local Teams connector slice:

- `sponsor.ask_question` delivery and stored human-question thread binding
- sponsor reply binding without fixture-supplied `bound_target_ref`
- unbound private threaded reply attention behavior
- default status redaction for private `status.reply` and
  `sponsor.ask_question` text through `delivery_records` and
  `safe_output_calls`

Files inspected:

- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_teams_connector_direct_messages.py`
- `tests/test_v2_teams_connector_human_questions.py`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed current branch `codex/v2-runtime-reset` and existing uncommitted Engineering changes. QA only edited this results file. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py` | Passed: 9 passed in 0.61s. |
| `pytest -q` | Passed: 25 passed in 1.38s. |
| Whole-snapshot privacy probe covering private DM source text, `status.reply` message text, `sponsor.ask_question` question/reason text, and sponsor answer text | Passed: `leaked_sentinels=[]`; `delivery_records[*].payload.body` was `[redacted private conversation]` for both `status.reply` and `sponsor.ask_question`; `safe_output_calls[*].payload.message/question/reason` were redacted with redaction flags; `human_question` and `teams_thread` bindings both targeted `work-story4-final-privacy`. |

## Retest Result

Pass. Story 4 mechanics still pass in the current working tree, and the prior
default-status privacy gap is closed for the tested local-adapter paths.

Default `status_snapshot()` no longer exposes private `status.reply` message
text or private `sponsor.ask_question` question/reason text through
`delivery_records` or `safe_output_calls`. The focused tests also continue to
prove stored `human_question` binding resolution and unbound private-thread
attention behavior.

## Residual Gaps

- Coverage remains local-adapter only; no real Teams tenant, Bot Framework,
  Graph, Entra consent, installation, permission, card, mention, or production
  thread-id evidence exists yet.
- Human loop-in participant audit, authority validation for who may answer, and
  channel/group-chat question semantics remain uncovered or deferred.
- Raw audit tables may still retain private payloads for future authorized
  debug paths; this retest covers the default status read model.

## Story 5 Gate

Story 5 may begin for the local Teams connector progression.

## Review Log

- RL-008 | qa-engineer | Story 4 final privacy retest | Focused Story 1-4
  connector tests, full test suite, and a whole-snapshot sentinel probe pass.
  Default status no longer leaks private `status.reply` or
  `sponsor.ask_question` text through `delivery_records` or
  `safe_output_calls`. Story 5 may begin for the local connector scope. |
  accepted 2026-06-12

# V2 Teams Connector Story 5 QA Results

Status: QA reviewed - rework required

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 5 - Delivery, Retry, Failure Attention, And Idempotency.

Files inspected:

- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_teams_connector_delivery_retry.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; current tree has modified Engineering files and untracked Story 5 test work. QA only edited this results file. |
| `pytest -q tests\test_v2_teams_connector_delivery_retry.py` | Passed: 3 passed in 0.25s. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py` | Passed: 12 passed in 0.83s. |
| `pytest -q` | Passed: 28 passed in 1.72s. |
| Forced failed `status.reply` safe-output probe using a subclassed local adapter | Failed Story 5 fake-claim expectation: a `status.reply` payload saying `I delivered the Teams reply successfully to the human.` was accepted while the delivery record ended in `failed_transient`; status output showed `delivery_statuses={"failed_transient": 1}` and one `delivery_failed_transient` attention item. |

## QA Decision

Story 5 does not pass the QA gate yet.

The delivery record, delivery attempt, duplicate outbound suppression,
transient retry, unknown outcome attention, permanent failure attention, retry
scheduling, and status visibility mechanics pass for the deterministic local
adapter scope. However, the safe-output delivery path still does not prevent a
role/status reply from claiming human Teams delivery succeeded when the
delivery attempt fails. Rework is required before Story 6.

## Acceptance Assessment

| Story 5 expectation | QA result |
| --- | --- |
| Delivery records and attempts are audited | Pass. New `delivery_attempts` records are created per send attempt and exposed in `status_snapshot()` counts/read model. |
| Duplicate outbound sends are suppressed idempotently | Pass. Duplicate `send_message()` with the same source, destination, and purpose returns the existing delivery id and does not add another attempt. |
| Transient failure can retry under policy | Pass. `failed_transient` creates retryable attention and can retry to `sent` without duplicating a successful send. |
| Unknown outcome is visible for operator review | Pass. Unknown outcome creates retryable attention with a warning to check Teams before retrying. |
| Permanent failure creates non-retryable attention | Pass. `failed_permanent` creates non-retryable operator attention and retry raises `ValueError`. |
| Retry scheduling is visible | Pass. `schedule_delivery_retry()` moves retryable records to `retry_scheduled`, and the status count reflects it. |
| Status/proof replies cannot claim human delivery succeeded when delivery failed | Fail. A forced failed `status.reply` safe-output call accepted message text claiming successful human delivery while the delivery record was `failed_transient`. |

## Coverage Assessment

Automated coverage is strong for local delivery-state mechanics:

- delivery record creation and attempt recording
- idempotent duplicate outbound suppression
- transient failure attention and retry to sent
- terminal behavior for already sent deliveries
- permanent failure non-retryability
- unknown outcome operator attention and retry scheduling visibility

Coverage is insufficient for the proof/status fake-claim gate. Current tests
exercise failed outcomes through `LocalTeamsTestAdapter.send_message()`
directly, but not through `ConnectorSafeOutputService.record()` with a
connector delivery failure. The generic safe-output fake-claim guard still
focuses on invented durable mutations such as work creation or deployment, not
human-visible Teams delivery-success claims.

## Required Rework

Minimum rework before Story 6:

- add validation or delivery-result wording control so `status.reply` and proof
  status messages cannot claim Teams/human delivery success unless the matching
  delivery record is actually `sent`
- add a regression test that forces `ConnectorSafeOutputService.record()` to
  experience a failed or unknown delivery and proves the role/status reply does
  not claim successful human delivery
- keep failed and unknown deliveries visible through delivery records,
  delivery attempts, and connector attention items

## Residual Gaps

- Story 5 remains local-adapter only; no real Teams tenant, Bot Framework,
  Graph, throttling, permission, timeout, or ambiguous network outcome evidence
  exists yet.
- `superseded` and `canceled` delivery states remain unimplemented or
  unexercised in this slice.
- Retry policy timing/backoff fields are represented only as state transitions;
  no `next_retry_at`, max-attempt, or scheduler worker behavior is covered yet.
- Attention resolution/closure after a successful retry remains future
  operator UX or dashboard scope.

## Review Log

- RL-009 | qa-engineer | Story 5 QA | Delivery attempts, duplicate outbound
  suppression, transient retry, unknown/permanent failure attention, retry
  scheduling, and status visibility pass local-adapter tests, but Story 5
  fails the fake-claim gate because a `status.reply` can still claim human
  Teams delivery succeeded when the connector delivery actually failed. |
  rework required 2026-06-12

# V2 Teams Connector Story 5 QA Retest

Status: QA retested current tree - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Retest Scope

Story 5 rework for the prior fake-claim blocker:

- `status.reply` and `status.complete` must not claim connector, Teams, or
  human delivery success.
- a failed `ConnectorSafeOutputService` `status.reply` delivery path must keep
  failed delivery evidence without accepting a false delivery-success claim.
- delivery retry and idempotency mechanics must remain green.

Files inspected:

- `src/agentic_mesh_v2/safe_outputs.py`
- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_safe_outputs.py`
- `tests/test_v2_teams_connector_delivery_retry.py`
- `docs/engineering/v2-teams-connector-implementation-log.md`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed current branch and existing uncommitted Engineering/QA changes. QA edited only this results file. |
| `pytest -q tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_delivery_retry.py` | Passed: 7 passed in 0.47s. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py` | Passed: 13 passed in 0.90s. |
| `pytest -q` | Passed: 30 passed in 1.64s. |
| Failed-delivery safe-output probe covering `status.reply` and `status.complete` delivery-success claims | Passed: one truthful `status.reply` was recorded, connector delivery ended `failed_transient`, one failed delivery attempt and one `delivery_failed_transient` attention item remained visible, the false `status.reply` delivery-success claim was rejected, the false `status.complete` delivery-success claim was rejected, and `safe_output_calls` stayed at 1. |

## Retest Decision

Story 5 now passes QA for the implemented local Teams connector scope.

The prior fake-claim blocker is fixed. Safe-output validation rejects
agent-authored delivery-success claims in both `status.reply` and
`status.complete`, and the failed connector delivery path preserves the real
runtime evidence instead of allowing a false human-visible success claim into
safe-output state.

## Acceptance Retest

| Story 5 expectation | Retest result |
| --- | --- |
| Delivery records and attempts are audited | Pass. Focused tests and probe show delivery records and delivery attempts are counted and visible. |
| Duplicate outbound sends are suppressed idempotently | Pass. Duplicate local adapter sends reuse the existing delivery record without adding another attempt. |
| Transient failure can retry under policy | Pass. `failed_transient` remains retryable and can transition to `sent` without duplicate successful sends. |
| Unknown outcome is visible for operator review | Pass. Unknown outcome remains retryable and warns operators to check Teams before retry. |
| Permanent failure creates non-retryable attention | Pass. Permanent failure attention is non-retryable and retry raises `ValueError`. |
| Retry scheduling is visible | Pass. `retry_scheduled` remains represented in delivery status counts. |
| `status.reply` cannot claim human/Teams delivery success | Pass. Regression test and probe reject the prior false success wording before a safe-output call is recorded. |
| `status.complete` cannot claim human/Teams delivery success | Pass. Probe confirmed the same delivery-success guard applies to `status.complete`. |
| Failed `ConnectorSafeOutputService` delivery keeps failure evidence | Pass. Probe left `delivery_statuses={"failed_transient": 1}`, one delivery attempt, and one `delivery_failed_transient` attention item while rejecting the false success claim. |

## Residual Gaps

- Coverage remains deterministic local-adapter coverage only; no real Teams
  tenant, Bot Framework, Graph, throttling, permission, timeout, or ambiguous
  network outcome evidence exists yet.
- The fake-claim guard is phrase-based validation, not a semantic proof system;
  future role prompt contracts and broader negative examples should keep
  tightening agent wording.
- `superseded` and `canceled` delivery states, retry backoff fields,
  max-attempt scheduling, and attention closure after successful retry remain
  future delivery/operator UX scope.

## Story 6 Gate

Story 6 may begin for the local Teams connector progression.

## Review Log

- RL-010 | qa-engineer | Story 5 QA retest | Focused safe-output and delivery
  retry tests, the Story 1-5 connector slice, full pytest, and a failed
  connector-delivery probe all pass. The prior false human/Teams delivery
  success claim is rejected for `status.reply` and `status.complete`, while
  failed delivery evidence remains visible. Story 6 may begin for the local
  connector scope. | accepted 2026-06-12

# V2 Teams Connector Story 6 QA Results

Status: QA reviewed - rework required

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 6 - Separate Visible Role Identities.

Files inspected:

- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_teams_connector_role_identities.py`
- Story 1-5 connector regression tests
- `docs/engineering/v2-teams-connector-implementation-log.md`
- `docs/engineering/v2-teams-connector-implementation-plan.md`
- `docs/qa/v2-teams-connector-test-plan.md`
- `docs/architecture/v2-teams-connector-architecture.md`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed current branch and existing uncommitted Engineering changes. QA edited only this results file. |
| `pytest -q tests\test_v2_teams_connector_role_identities.py` | Passed: 4 passed in 0.26s. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py` | Passed: 17 passed in 1.16s. |
| `pytest -q` | Passed: 34 passed in 2.05s. |
| `python -m agentic_mesh_v2.cli --db .tmp\v2-story6-qa.sqlite3 init-db` then `python -m agentic_mesh_v2.cli --db .tmp\v2-story6-qa.sqlite3 status-json` | Passed; migration/status smoke returned an empty status snapshot with connector counts present. |
| Direct-message identity fallback probe with one enabled role and one disabled role | Failed Story 6 gate: a DM explicitly targeted at disabled `release-manager` via `target_role_id` and `target_ref=bot-release-manager` created an assignment for enabled `product-manager` and no attention item. |
| Display-name-only direct-message probe with one enabled role | Failed Story 6 gate: a DM using `target_ref=AM-Product Manager` created a `product-manager` assignment and no attention item because the single-enabled-role fallback overrode the untrusted target ref. |
| Disabled-role outbound delivery probe | Failed disablement expectation: `adapter.send_message(... role_id="release-manager")` created a sent delivery with `role_identity.enabled=false` and no attention item. |

## QA Decision

Story 6 does not pass the QA gate yet.

The checked-in focused tests pass and provide useful coverage for explicit
role identity config, participant presentation metadata, alias/mention-handle
resolution, display-name-only channel text, disabled channel mentions, outbound
identity metadata, bad identity models, and duplicate external refs.

Rework is required because direct-message routing still falls back to the only
enabled role even when the inbound event carries an explicit disabled or
display-name-only target. Emergency disablement also does not currently block
outbound delivery for the disabled role identity.

## Acceptance Assessment

| Story 6 expectation | QA result |
| --- | --- |
| Config supports separate app/bot, shared gateway, and hybrid identity models | Pass for local config parsing. The accepted model values are covered by tests. |
| Each configured role can expose display name, alias, mention handle, external app/bot id, identity model, and enabled flag | Pass. Role participants preserve display name/external ref and metadata in the status snapshot. |
| Outbound delivery includes configured role identity metadata | Pass for enabled identities. Delivery payloads include role id, external ref, display name, alias, mention handle, identity model, and enabled flag. |
| Runtime authorization does not rely on display name alone | Fail. Display-name-only DM target input can route through the single-enabled-role fallback. Channel plain-text display names are covered, but DM target handling is not fail-closed. |
| Disabled role identity creates attention and no assignment | Partial/fail. Disabled channel mentions create `disabled_role_identity` attention and no assignment, but direct messages explicitly targeting a disabled role can be misassigned to another enabled role. |
| Emergency disablement blocks the role identity | Fail. A disabled role can still send a `sent` outbound delivery through `send_message()` with no attention item. |
| Regression impact on Stories 1-5 | No automated regression detected in the focused connector slice or full pytest suite; the failing probes are Story 6 identity edge cases not covered by the current tests. |

## Required Rework

- Make direct-message target resolution fail closed when `target_role_id` or
  `target_ref` is present but maps to a disabled, unknown, or display-name-only
  identity. Create operator attention instead of falling back to another role.
- Add regression tests for disabled direct-message targets and display-name-only
  direct-message targets, including the single-enabled-role configuration.
- Decide and enforce the outbound emergency-disablement contract. If
  `enabled=false` means the role identity is disabled, outbound sends for that
  role should fail closed with attention rather than recording a sent delivery.
- Keep the single-role DM fallback only for events with no explicit target
  identity, if that fallback is still desired for the local adapter.

## Residual Gaps

- Story 6 remains local-adapter coverage only. There is no real Teams tenant,
  Bot Framework, Graph, Entra consent, installation, app registration, or
  permission evidence yet.
- The QA plan still records a broader Story 6/12 dependency on Security and
  Engineering selecting the first real Teams identity model, permission set,
  app owner model, credential rotation path, and emergency disablement
  procedure.
- Connector-level disablement is not implemented or covered in this slice.

## Story 7 Gate

Story 7 should not begin yet. Story 6 needs rework and retest for fail-closed
direct-message identity routing and disabled outbound identity behavior.

## Review Log

- RL-011 | qa-engineer | Story 6 QA | Focused identity tests, Story 1-6
  connector regression tests, full pytest, and status-json smoke pass, but
  direct-message target fallback can route disabled or display-name-only
  targets to another enabled role, and disabled role identities can still send
  outbound deliveries. Story 7 should wait for rework and retest. | rework
  required 2026-06-12

# V2 Teams Connector Story 6 QA Retest

Status: QA retested current tree - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Retest Scope

Story 6 rework for the prior role identity blockers:

- explicit direct message to disabled `release-manager` must not assign work to
  enabled `product-manager`
- display-name-only direct-message target must not fall back to an enabled role
- disabled role identity must not send outbound delivery
- prior role identity behavior must remain green

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed current branch and existing uncommitted Engineering/QA changes. QA edited only this results file. |
| `pytest -q tests\test_v2_teams_connector_role_identities.py` | Passed: 6 passed in 0.36s. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py` | Passed: 19 passed in 1.25s. |
| `pytest -q` | Passed: 36 passed in 2.08s. |

## Retest Decision

Story 6 now passes QA for the implemented local Teams connector scope.

The prior blockers are fixed in the focused regression tests. Explicit
disabled-role direct-message targets and display-name-only direct-message
targets now create operator attention without creating role assignments, and
disabled role outbound delivery raises before any delivery record or attempt is
created. Existing role identity metadata, alias/mention routing, enabled
outbound identity metadata, bad identity model validation, and duplicate
external-ref validation still pass.

## Residual Gaps

- Coverage remains deterministic local-adapter coverage only; no real Teams
  tenant, Bot Framework, Graph, Entra consent, installation, app registration,
  permission, or production identity evidence exists yet.
- Connector-level disablement and the broader real-tenant emergency
  disablement operating procedure remain future Story 6/12 follow-up work.
- Display-name-only safety is covered for the local adapter DM target path and
  channel plain-text path; real Teams mention entity parsing still needs tenant
  validation later.

## Story 7 Gate

Story 7 may begin for the local Teams connector progression.

## Review Log

- RL-012 | qa-engineer | Story 6 QA retest | Focused role identity tests,
  Story 1-6 connector regression tests, and full pytest pass. The prior
  disabled direct-message misassignment, display-name-only direct-message
  fallback, and disabled outbound delivery blockers are fixed. Story 7 may
  begin for the local connector scope. | accepted 2026-06-12

# V2 Teams Connector Story 7 QA Results

Status: QA reviewed - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 7 - Feature, Epic, Incident, And Focused-Work Channels.

Files inspected:

- `src/agentic_mesh_v2/connectors.py`
- `tests/test_v2_teams_connector_focus_channels.py`
- Story 1-6 connector regression tests
- `docs/engineering/v2-teams-connector-implementation-log.md`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` with existing Engineering changes in `docs/engineering/v2-teams-connector-implementation-log.md`, `src/agentic_mesh_v2/connectors.py`, and untracked `tests/test_v2_teams_connector_focus_channels.py`. QA edited only this results file. |
| `pytest -q tests\test_v2_teams_connector_focus_channels.py` | Passed: 4 passed in 0.24s. |
| `pytest -q tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py` | Passed: 23 passed in 1.40s. |
| `pytest -q` | Passed: 40 passed in 2.15s. |
| `python -m agentic_mesh_v2.cli --db .tmp\v2-story7-qa.sqlite3 init-db` then `python -m agentic_mesh_v2.cli --db .tmp\v2-story7-qa.sqlite3 status-json` | Passed; migration/status smoke returned an empty status snapshot with connector counts present. |
| In-memory Story 7 scope/visibility probe through `ConnectorConfig`, `LocalTeamsTestAdapter`, and `status_snapshot()` | Passed. Feature, epic, incident, focused-work, and default project channel events carried expected `channel_scope`; focused-work mention created one role assignment with `work_scope=focus-abc`; unbound private channel created one `unbound_private_channel` attention item and no extra assignment. Probe counts included `conversation_events=6`, `role_assignments=1`, `connector_attention_items=1`, `queue_items=0`, and `work_items=0`. |

## QA Decision

Story 7 passes QA for the implemented local Teams connector scope.

The implementation supports explicit channel bindings for feature, epic,
incident, and focused-work contexts; records scoped channel metadata on
conversation events; carries the same scope into role-mention assignments; keeps
focused-channel mentions and threads on the default project-channel routing
rules; exposes configured channel bindings through connector health; and blocks
unbound private-channel routing by creating operator attention without creating
role assignments.

## Acceptance Assessment

| Story 7 expectation | QA result |
| --- | --- |
| Feature, epic, incident, and focused-work channel bindings are accepted | Pass. Config parsing accepts the allowed scope types, and the extra probe exercised all four. |
| Scoped channel context is visible in conversation events | Pass. Focused tests and probe show `channel_scope` metadata including `scope_type`, `visibility`, `display_name`, and `work_scope`. |
| Focus-channel role mentions follow default channel assignment rules | Pass. Focused-channel mention creates `channel_role_mention` assignment without queue/work-item inference. |
| Focus-channel thread bindings follow default channel thread rules | Pass. Focused tests verify source thread binding for role mentions; probe also created focused/default thread bindings. |
| Channel binding visibility and work scope remain visible | Pass. Tests and probe verified project, restricted, and private visibility plus work-scope propagation into events and assignments. |
| Private channels require explicit binding | Pass. Bound private incident channel routed normally; unbound private channel produced `unbound_private_channel` attention and no role assignment. |
| Regression impact on Stories 1-6 | Pass. Story 1-7 connector regression tests and full pytest suite remained green. |

## Residual Gaps

- Coverage remains deterministic local-adapter coverage only; no real Teams
  tenant, Graph private-channel membership, Bot Framework permission, consent,
  or real channel installation evidence exists yet.
- Runtime creation or discovery of Teams channels is intentionally out of scope;
  Story 7 depends on explicit configured bindings.
- Focus-channel metadata is exposed through JSON/status read models, but richer
  dashboard presentation remains future UI scope.
- Current tests cover invalid default-channel duplication and bad scope. Bad
  visibility, duplicate focus-channel refs, and non-list binding shape are
  enforced in code but not each represented by a named focused test.

## Story 8 Gate

Story 8 may begin for the local Teams connector progression.

## Review Log

- RL-013 | qa-engineer | Story 7 QA | Focused channel tests, Story 1-7
  connector regression tests, full pytest, CLI status smoke, and an extra
  scope/visibility probe all pass. Feature, epic, incident, focused-work, and
  private-channel binding behavior is acceptable for the local connector
  scope. Story 8 may begin. | accepted 2026-06-12

# V2 Teams Connector Story 8 QA Results

Status: QA reviewed - pass with residual gap

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Scope Reviewed

Story reviewed: Story 8 - Team-Wide Relevance Checks.

Files inspected:

- `src/agentic_mesh_v2/connectors.py`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/safe_outputs.py`
- `tests/test_v2_teams_connector_team_wide_relevance.py`
- Story 1-7 connector regression tests
- `docs/engineering/v2-teams-connector-implementation-log.md`
- `docs/engineering/v2-teams-connector-implementation-plan.md`
- `docs/qa/v2-teams-connector-test-plan.md`

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` with existing Engineering changes and the new Story 8 test file. QA edited only this results file. |
| `pytest -q -p no:cacheprovider tests\test_v2_teams_connector_team_wide_relevance.py` | Passed: 3 passed in 0.30s. |
| `pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_team_wide_relevance.py` | Passed: 6 passed in 0.44s. |
| `pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py` | Passed: 26 passed in 1.68s. |
| `pytest -q -p no:cacheprovider` | Passed: 43 passed in 2.50s. |
| In-memory duplicate/no-op probe through `LocalTeamsTestAdapter`, `ConnectorSafeOutputService`, and `status_snapshot()` | Passed for duplicate suppression and visibility: first route `team_wide_prompt`, duplicate replay `duplicate=True`, `conversation_events=1`, `role_assignments=3`, enabled assignment roles `engineering`, `product-manager`, and `qa-engineer`, and `relevance_checks=1`. Probe also exposed the residual gap that a later `status.reply` after a no-op relevance record is accepted and creates one delivery record. |

## QA Decision

Story 8 passes QA for the implemented local Teams connector scope.

The implementation creates team-wide relevance assignments for every enabled
role, skips disabled role identities, persists `relevance.record` decisions
with score, threshold, decision, reason, no-op, exception, safe-output, and
delivery refs, keeps no-op relevance records silent in the tested flow, and
keeps role-to-role follow-up in runtime safe-output (`consult.request`) rather
than Teams delivery.

This is not complete connector release approval. The actual relevance evaluator
and prompt behavior remain outside this slice, and no real Teams tenant
evidence exists yet.

## Acceptance Assessment

| Story 8 expectation | QA result |
| --- | --- |
| Team-wide trigger creates a `team_wide_prompt` record | Pass. Focused tests and probe show route type `team_wide_prompt` on the conversation event. |
| Every enabled role receives relevance work | Pass. Product Manager, Engineering, and QA Engineer receive `team_wide_relevance_check` assignments; disabled Release Manager is skipped. |
| Relevance records persist score, threshold, decision, reason, no-op, exception reason, safe-output ref, and delivery ref | Pass. `relevance_checks` are exposed in `status_snapshot()`, and focused tests cover no-op, material, and exception records. |
| Non-relevant roles stay quiet | Pass for the compliant tested flow: recording `decision=not_relevant` with `noop=true` creates no delivery record. |
| Material or justified-exception roles may reply with delivery evidence | Pass for local adapter evidence. Material input can pair with `status.reply`; exception records can store a delivery ref and exception reason. |
| Role-to-role follow-up uses runtime consult, not Teams transport | Pass. The focused regression records `consult.request` and creates zero delivery records. |
| Duplicate team-wide trigger does not duplicate side effects | Pass in extra probe: duplicate replay reused the receipt and left one conversation event plus one assignment per enabled role. |

## Residual Gaps

- No connector-side guard currently prevents a role from recording
  `not_relevant`/`noop=true` and then later emitting `status.reply` to the same
  conversation. The quiet behavior is proven for the intended flow, but not
  enforced as a hard channel-noise invariant.
- The actual relevance scoring worker/prompt contract is still not
  implemented; Story 8 provides the runtime record and persistence surface.
- Threshold selection is currently the local adapter default `0.6`; project
  configuration and evaluator calibration remain future work.
- Coverage remains deterministic local-adapter coverage only; no real Teams
  tenant, Bot Framework, Graph, Entra, permission, throttling, or real
  team-wide mention evidence exists yet.
- `consult.request` is represented as runtime safe-output evidence in this
  slice; richer consult routing/claiming remains runtime work outside this
  local connector story.

## Story 9 Gate

Story 9 may begin for the local Teams connector progression.

Before the complete Teams connector release, Engineering should either add a
runtime/connector guard or explicitly document the prompt/runtime enforcement
boundary for suppressing `status.reply` after a no-op relevance decision.

## Review Log

- RL-014 | qa-engineer | Story 8 QA | Focused team-wide relevance tests,
  safe-output tests, Story 1-8 connector regression tests, full pytest, and an
  extra duplicate/no-op probe pass for the local connector scope. Story 9 may
  begin, with a tracked residual gap that no-op silence is not yet a hard
  connector-side guard against later `status.reply` noise. | accepted
  2026-06-12

# V2 Teams Connector Story 8 QA Retest

Status: QA retested current tree - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Retest Scope

Story 8 rework for the prior residual no-op relevance gap:

- a run that records `decision=not_relevant` with `noop=true` must not later
  post a Teams `status.reply`
- the rejected no-op reply must not create a delivery record
- material and exception relevance replies must remain allowed
- runtime consult follow-up must remain runtime-only and create no Teams
  delivery

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` with existing Engineering changes. QA edited only this results file. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_team_wide_relevance.py` | Passed: 4 passed in 0.39s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_team_wide_relevance.py` | Passed: 7 passed in 0.54s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py` | Passed: 27 passed in 1.74s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` | Passed: 44 passed in 2.60s. |

## Retest Decision

Story 8 now passes QA without the prior no-op residual gap for the implemented
local Teams connector scope.

The focused regression `test_noop_relevance_decision_cannot_post_teams_reply_in_same_run`
proves that after a `not_relevant`/`noop=true` relevance record, a same-run
`status.reply` is rejected with a no-op relevance error, the safe-output count
stays at the original relevance record, and `delivery_records` remains zero.
The same focused file still proves material reply delivery evidence, exception
delivery references, and runtime `consult.request` follow-up with zero Teams
delivery.

## Residual Gaps

- Coverage remains deterministic local-adapter coverage only; no real Teams
  tenant, Bot Framework, Graph, Entra, permission, throttling, or real
  team-wide mention evidence exists yet.
- The actual relevance scoring worker/prompt contract is still outside this
  slice; Story 8 covers persistence, guardrails, and connector behavior.
- Threshold calibration and project-configurable relevance policy remain future
  work.

## Story 9 Gate

Story 9 may begin for the local Teams connector progression.

## Review Log

- RL-015 | qa-engineer | Story 8 QA retest | Focused Story 8 tests,
  safe-output regression tests, Story 1-8 connector regression tests, and full
  pytest pass. The prior no-op residual gap is closed: same-run no-op
  relevance decisions cannot post Teams `status.reply` or create delivery
  records. Story 9 may begin for the local connector scope. | accepted
  2026-06-12

# V2 Teams Connector Story 9 QA Review

Status: QA reviewed current tree - changes requested

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Review Scope

Story 9 - Proactive Work Proposals And Conversation Promotion:

- durable queue proposals must be created only through `queue.propose_item`
- ordinary Teams channel and DM free text must not infer work
- private DM promotion must preserve source references, classification, and
  redaction without raw body leakage
- `status.reply` may reference only proposal/work that actually exists
- fake/raw conversation claims must be rejected

QA did not edit source. QA appended this result file only.

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` with existing Story 9 source/test changes. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_work_proposals.py` | Passed: 4 passed in 0.40s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_work_proposals.py` | Passed: 7 passed in 0.59s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py tests\test_v2_teams_connector_work_proposals.py` | Passed: 31 passed in 2.29s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` | Passed: 48 passed in 3.23s. |
| In-memory fake source probe: `queue.propose_item` with `source_conversation_event_id='conversation-event-fake'` | Rejected with `ValueError`, but left `safe_output_calls=1`, `queue_items=0`, `work_proposals=0`. |
| In-memory fake work reference probe: `status.reply` with `work_item_id='work-not-real'` | Accepted and created `safe_output_calls=1`, `delivery_records=1`. |

## Decision

Changes requested for Story 9 before QA acceptance.

The core happy path is present and the regression suite passes: `queue.propose_item`
creates a durable queued proposal, ordinary project-channel free text remains
context only, checked-in tests prove private source body text is absent from
default status output, and raw `body`/`message`/`raw_text`/`raw_message`
proposal payloads are rejected.

Two acceptance guardrails are still incomplete.

## Findings

- P1 - `status.reply` accepts fake work references. The Story 9 acceptance
  rule says `status.reply` can reference only proposal/work that actually
  exists, but `ConnectorSafeOutputService._validate_reply_references()` checks
  only `queue_item_id`. A `status.reply` payload with
  `work_item_id='work-not-real'` was accepted and delivered. Relevant code:
  `src/agentic_mesh_v2/connectors.py` lines 682-685. `V2Database.get_work_item()`
  already raises for unknown work IDs and can support this guard.

- P1 - rejected fake conversation sources leave durable safe-output evidence.
  `ConnectorSafeOutputService.record()` records the safe-output call before
  validating and recording the proposal-specific conversation source. A
  `queue.propose_item` with unknown `source_conversation_event_id` raises
  `ValueError`, but the rejected call remains in `safe_output_calls`. That
  leaves audit state implying the role emitted a terminal queue proposal even
  though the source claim was fake and no proposal exists. Relevant code:
  `src/agentic_mesh_v2/connectors.py` lines 656-660 and 687-694.

## Passing Evidence

| Story 9 expectation | QA result |
| --- | --- |
| Role can create durable queue proposal only via `queue.propose_item` | Pass for the implemented path. Focused tests show one queued item and one work proposal only after `queue.propose_item`. |
| Ordinary Teams/channel/DM free text does not infer work | Pass for checked project-channel regression; prior Story 2 DM regression still proves ordinary DM reply creates no queue/work item. |
| Private DM promotion stores source references, classification, redaction, and no raw body in default status | Pass for the checked-in sentinel test: proposal stores source conversation/event/receipt refs, classification, `private_source_redacted`, and no `PRIVATE_SENTINEL` in `status_snapshot()`. |
| Raw conversation payload fields are rejected from queue proposals | Pass for `body`/`message`/`raw_text`/`raw_message` guard coverage in `safe_outputs.py`. |
| `status.reply` unknown queue reference is rejected | Pass for `queue_item_id='queue-not-real'`. |

## Residual Gaps

- Real Teams tenant, Bot Framework, Graph, Entra, permissions, throttling, and
  real private DM promotion evidence remain outside this local-adapter story.
- The checked-in raw-text rejection guard is field-name based. It does not
  classify arbitrary copied private content placed into allowed fields such as
  `summary` or `rationale`; this may be acceptable as prompt/policy territory,
  but it remains a privacy residual until a stronger content-redaction policy
  exists.
- The safe-output front door still returns only a call id; direct returned
  proposal/queue refs are deferred per Engineering notes.

## Story 10 Gate

Story 10 should not begin until the two P1 Story 9 findings above are fixed
or explicitly waived by the sponsor.

## Review Log

- RL-016 | qa-engineer | Story 9 QA | Focused Story 9 tests, safe-output
  regressions, Story 1-9 local connector regressions, full pytest, and two
  extra fake-reference probes were run. Tests pass, but QA requests changes:
  `status.reply` accepts fake work item references, and rejected
  `queue.propose_item` fake source claims leave durable safe-output records. |
  changes requested 2026-06-12

# V2 Teams Connector Story 9 QA Retest

Status: QA retested current tree - pass

Owner role: QA Engineer

Date: 2026-06-12

Branch: `codex/v2-runtime-reset`

## Retest Scope

Story 9 rework for the prior QA findings:

- `status.reply` must reject unknown `work_item_id` references before
  safe-output recording or Teams delivery
- `queue.propose_item` must reject unknown `source_conversation_event_id`
  before safe-output persistence
- focused regressions must continue proving no work inference from ordinary
  Teams text, private promotion redaction/source refs, queue proposal creation,
  and raw conversation text rejection

QA did not edit source. QA appended this result file only.

## Commands Run

| Command | Result |
| --- | --- |
| `git status --short --branch` | Passed; confirmed branch `codex/v2-runtime-reset` with existing Story 9 rework changes. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_work_proposals.py` | Passed: 5 passed in 0.47s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_safe_outputs.py tests\test_v2_teams_connector_work_proposals.py` | Passed: 8 passed in 0.66s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_teams_connector_foundation.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_teams_connector_project_channels.py tests\test_v2_teams_connector_human_questions.py tests\test_v2_teams_connector_delivery_retry.py tests\test_v2_teams_connector_role_identities.py tests\test_v2_teams_connector_focus_channels.py tests\test_v2_teams_connector_team_wide_relevance.py tests\test_v2_teams_connector_work_proposals.py` | Passed: 32 passed in 2.30s. |
| `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` | Passed: 49 passed in 3.11s. |

## Retest Decision

Story 9 passes QA for the implemented local Teams connector scope.

The prior P1 findings are closed. The focused rework proves that an unknown
`work_item_id` on `status.reply` is rejected before delivery, and an unknown
`source_conversation_event_id` on `queue.propose_item` is rejected before any
safe-output call, queue item, or work proposal is persisted. The full Story
1-9 local connector regression pack and full test suite also pass.

## Residual Gaps

- Coverage remains deterministic local-adapter coverage only; no real Teams
  tenant, Bot Framework, Graph, Entra, permission, throttling, or live private
  DM promotion evidence exists yet.
- Raw conversation rejection is still field-name based. It blocks explicit raw
  payload fields such as `body`, `message`, `raw_text`, and `raw_message`, but
  does not classify arbitrary copied private text placed into allowed fields
  such as `summary` or `rationale`.
- The safe-output front door still returns a call id rather than direct
  queue/proposal refs; Engineering has already noted CLI/MCP wrappers should
  expose those refs when those front doors are implemented.

## Story 10 Gate

Story 10 may begin for the local Teams connector progression.

## Review Log

- RL-017 | qa-engineer | Story 9 QA retest | Focused Story 9 tests,
  safe-output pairing, Story 1-9 local connector regressions, and full pytest
  pass. Prior P1 findings are closed: `status.reply` now rejects unknown work
  references before recording/delivery, and `queue.propose_item` rejects fake
  source conversation events before safe-output persistence. Story 10 may
  begin for the local connector scope. | accepted 2026-06-12
