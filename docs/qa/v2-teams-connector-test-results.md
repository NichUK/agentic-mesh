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
