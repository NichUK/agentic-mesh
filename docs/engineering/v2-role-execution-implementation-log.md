# V2 Role Execution Implementation Log

Status: in progress

Owner role: engineering

Date started: 2026-06-12

Source documents:

- `docs/product/backlog.md`
- `docs/architecture/v2-runtime-reset.md`
- `docs/architecture/agentic-mesh-design.md`

## PB-004 Story 1 - Claim And Run Role Assignments

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add the first runtime execution bridge for PB-004. Connector and runtime code
can already create role assignments; this story lets a role service claim one
queued assignment for its role, run the worker through the safe-output contract,
and record terminal assignment state without using Teams as agent-to-agent
transport.

### Implementation Notes

- Added role-assignment claim metadata:
  - `claimed_at`
  - `completed_at`
  - `run_id`
  - `terminal_tool`
  - `failure_reason`
- Added idempotent migration guards for existing v2 SQLite databases.
- Added repository methods:
  - `claim_role_assignment()`
  - `get_role_assignment()`
  - extended `complete_role_assignment()`
  - `fail_role_assignment()`
- Added `RoleService.claim_next_assignment()` and
  `RoleService.run_next_assignment()`.
- Role services claim only queued assignments for their own role.
- Successful worker runs complete the assignment with run id and terminal
  safe-output tool.
- Worker failures or invalid safe-output runs mark the claimed assignment
  failed and preserve the failed agent run id.
- Status snapshots now expose `role_assignment_statuses`.
- The status dashboard now renders role assignments with role instance, status,
  terminal tool, source/work reference, run id, and failure reason.

### Tests Added

- connector-created direct-message assignment can be claimed by Product Manager
  and completed through `status.reply`
- a Product Manager service does not claim Engineering assignments
- a worker that emits no safe-output marks the claimed assignment failed
- status dashboard renders completed role assignment state

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
8 passed
79 passed
```

### Known Limitations

- This story claims and executes one assignment at a time. A continuous
  long-running service loop, scheduling policy, lease timeout, heartbeat
  refresh, and recovery scan remain later PB-004/PB-005 slices.
- This story does not yet auto-create downstream role assignments from
  lifecycle handoff safe-output calls.
- Prompt/context assembly from source documents, role memory, and flow state is
  still represented by assignment payload fields and remains a later slice.

## PB-004 Story 2 - Materialize Handoff And Consult Assignments

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Convert internal `handoff.request` and `consult.request` safe-output calls into
queued downstream role assignments. This keeps role-to-role coordination inside
runtime state instead of using Teams as agent-to-agent transport.

### Implementation Notes

- Added `V2Database.get_agent_run()` so route materialization can inherit the
  source work item from the running role.
- Extended shared `SafeOutputService.record()` so `handoff.request` and
  `consult.request` create queued role assignments after the safe-output call
  is validated and recorded.
- Handoff/consult assignments preserve:
  - source safe-output call id
  - source run id
  - source role
  - target role
  - inherited work item id
  - reason
  - route tool
  - current flow state
  - source document refs
  - target outputs
  - target role safe-output tool list
  - context visibility
- Internal route assignment creation does not create Teams delivery records.

### Tests Added

- `handoff.request` from Product Manager creates one queued Engineering
  assignment while completing the source Product Manager assignment.
- `consult.request` from Engineering creates one queued QA Engineer consult
  assignment with inherited work item and target output context.
- Handoff/consult routing creates no Teams delivery records.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_safe_outputs.py tests\test_v2_end_to_end.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
10 passed
81 passed
```

### Known Limitations

- Handoff and consult assignments are created from explicit safe-output calls;
  this story does not infer handoffs from prose.
- This story does not yet drive lifecycle state transitions automatically.
- Assignment recovery, leases, continuous role-service loops, and hibernation
  remain later PB-004/PB-005 slices.

## Review Log

- RL-001 | engineering | implementation | PB-004 Story 1 | Added
  claim-and-run role assignment execution with terminal assignment state and
  dashboard visibility. | QA accepted 2026-06-12
- RL-002 | engineering | implementation | PB-004 Story 2 | Added
  safe-output-driven handoff/consult assignment materialization with inherited
  work item context and no Teams delivery side effects. | awaiting QA review
  2026-06-12
