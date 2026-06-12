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
