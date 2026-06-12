# V2 Role Execution Test Results

## PB-004 Story 1 - Claim And Run Role Assignments

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/role_service.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_role_assignment_execution.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_teams_connector_direct_messages.py tests\test_v2_status_dashboard.py
```

Result:

```text
8 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
79 passed
```

### Acceptance Assessment

- Role services can claim queued assignments for their own role.
- Role services do not claim assignments for other roles.
- Claimed assignments execute through the safe-output service.
- Successful runs record completed assignment state with run id and terminal tool.
- Invalid worker runs that emit no terminal safe-output mark the claimed assignment failed and preserve the failed run.
- Status snapshot and dashboard expose role assignment counts and current/terminal assignment rows.

### Residual Gaps

These are not blockers for PB-004 Story 1 because the engineering implementation log explicitly leaves them for later PB-004/PB-005 slices:

- No continuous long-running role-service loop yet.
- No lease timeout, claim recovery, or heartbeat refresh yet.
- No hibernation/hydration behavior yet.
- No automatic lifecycle handoff assignment creation yet.
- Prompt, memory, and flow-state context assembly is still represented through assignment payload data.

### Review Log

- QA-RL-001 | qa-engineer | acceptance | PB-004 Story 1 | Verified role assignment claim/run behavior, terminal state recording, other-role isolation, and dashboard/status visibility. | accepted 2026-06-12

## PB-004 Story 2 - Materialize Handoff And Consult Assignments

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/safe_outputs.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_role_assignment_execution.py`
- `docs/engineering/v2-role-execution-implementation-log.md`
- Prior QA record in `docs/qa/v2-role-execution-test-results.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_safe_outputs.py tests\test_v2_end_to_end.py
```

Result:

```text
10 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
81 passed
```

### Acceptance Assessment

- `handoff.request` creates a queued target role assignment with the inherited work item, source run, source safe-output call reference, source role, target outputs, target allowed tools, and current flow/context payload.
- `consult.request` creates a queued consult assignment with inherited work item context and target output expectations.
- The source role assignment can complete while the downstream assignment remains queued for the target role.
- Internal handoff and consult routes create no Teams delivery records.
- The implementation log correctly limits this story to route materialization and does not claim continuous loops, lease recovery, hibernation, or lifecycle state automation.

### Residual Gaps

These are not blockers for PB-004 Story 2 because they are outside this story and remain explicit later PB-004/PB-005 scope:

- No continuous role-service claim loop yet.
- No lease timeout, recovery polling, or hibernation/hydration behavior yet.
- No automatic lifecycle state transition automation yet.
- Prompt, memory, and richer flow-state context assembly remains represented through assignment payload data.

### Review Log

- QA-RL-002 | qa-engineer | acceptance | PB-004 Story 2 | Verified safe-output-driven handoff/consult assignment materialization, inherited work item context, target role tool/context payload, source assignment completion, and absence of Teams delivery side effects. | accepted 2026-06-12

## PB-004 Story 3 - Terminal Assignment Outcome States

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/safe_outputs.py`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/role_service.py`
- `tests/test_v2_role_assignment_execution.py`
- `docs/engineering/v2-role-execution-implementation-log.md`
- Prior QA record in `docs/qa/v2-role-execution-test-results.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_safe_outputs.py tests\test_v2_end_to_end.py
```

Result:

```text
14 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
85 passed
```

### Acceptance Assessment

- `noop` is a first-class terminal safe-output tool available to common role tooling and requires a non-empty `reason`.
- `report.blocked` maps a claimed assignment to the visible `blocked` state.
- `sponsor.ask_question`, `human_response.request`, and `release.request_approval` map claimed assignments to `waiting_human`.
- `report.incomplete` maps a claimed assignment to `incomplete`.
- Completed terminal outcomes still complete claimed assignments and preserve the terminal tool/run id.
- `role_assignment_statuses` exposes terminal assignment status counts in the status snapshot.
- The implementation log accurately limits this story to visible terminal state mapping and does not claim scheduler, recovery, hibernation, or human-response processing behavior.

### Residual Gaps

These are not blockers for PB-004 Story 3 because they are outside this story and remain explicit later PB-004/PB-005 scope:

- No automatic retry/recovery loop for `blocked` or `incomplete` assignments yet.
- No scheduler processing for `waiting_human` assignments yet.
- No long-running role service loop, lease recovery, or hibernation/hydration behavior yet.

### Review Log

- QA-RL-003 | qa-engineer | acceptance | PB-004 Story 3 | Verified terminal safe-output validation and assignment state mapping for completed, waiting-human, blocked, incomplete, and no-op outcomes with status snapshot visibility. | accepted 2026-06-12

## PB-004 Story 4 - Bounded Role-Service Drain And Heartbeat

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/role_service.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_role_assignment_execution.py`
- `docs/engineering/v2-role-execution-implementation-log.md`
- Prior QA record in `docs/qa/v2-role-execution-test-results.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
16 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
88 passed
```

### Acceptance Assessment

- `role_instance_status` exists as a durable runtime table with role, instance, status, heartbeat, current assignment, last run, processed count, and detail fields.
- `RoleService` initializes the role instance as `idle`.
- `run_next_assignment()` marks the role instance `active` while processing claimed work, returns it to `idle` after terminal assignment completion, and marks it `failed` when worker or safe-output execution fails.
- `drain_available_assignments(max_assignments=...)` respects the configured bound and records the processed batch count.
- Status snapshots and the dashboard expose role instance state, current assignment, last run, processed count, heartbeat time, and detail.
- The implementation log accurately limits this story to bounded in-process draining and heartbeat/status visibility; it does not claim a container supervisor, claim lease timeout, stale-claim recovery, or hibernation/hydration.

### Residual Gaps

These are not blockers for PB-004 Story 4 because they remain explicit later v2 runtime scope:

- No container supervisor or persistent service process orchestration yet.
- No lease timeout or stale-claim recovery scan yet.
- No cooperative hibernation/hydration behavior yet.
- `processed_count` is the latest drain batch count, not a lifetime processed metric.

### Review Log

- QA-RL-004 | qa-engineer | acceptance | PB-004 Story 4 | Verified bounded role-service draining, role-instance heartbeat/status records, processed-count visibility, failure-state handling, and dashboard/status exposure. | accepted 2026-06-12

## PB-004 Story 5 - Assignment Lease And Stale Claim Recovery

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/role_service.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_role_assignment_execution.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
20 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
92 passed
```

### Acceptance Assessment

- Claimed role assignments now receive `claim_expires_at` and retain a `recovery_count`.
- Lease refresh is scoped to the claiming role instance and fails for a different role instance.
- Assignment completion and failure both clear `claim_expires_at`.
- Stale claimed assignments can be explicitly recovered to `queued`, clear ownership and claim timestamps, record the recovery reason/count, and append a `role_assignment.recovered` event.
- Stale recovery rechecks lease conditions during the update before recording the recovery event.
- Reclaiming recovered queued work clears stale `completed_at`, `run_id`, `terminal_tool`, and `failure_reason` while preserving `recovery_count`.
- Unexpired claims are not recovered by the explicit recovery operation.
- Status snapshots and the dashboard expose lease expiry and recovery count for role assignments.
- The implementation remains explicit operation support only; it does not claim automatic scheduler polling, container hibernation, or hydration behavior.

### Residual Gaps

These are not blockers for PB-004 Story 5 because they remain later v2 runtime scope:

- No automatic stale-claim recovery scheduler yet.
- No distributed lease coordination beyond the SQLite-backed update checks.
- No container hibernation/hydration behavior yet.

### Review Log

- QA-RL-005 | qa-engineer | acceptance | PB-004 Story 5 | Verified assignment lease creation/refresh, terminal lease clearing, stale-claim recovery, recovered-work reclaim cleanup, event/audit evidence, dashboard visibility, and explicit non-claim of scheduler or hibernation support. | accepted 2026-06-12

## PB-004 Story 6 - Role-Service Maintenance Tick

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/role_service.py`
- `tests/test_v2_role_assignment_execution.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
22 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
94 passed
```

### Acceptance Assessment

- Stale assignment recovery can be scoped to one role through `recover_stale_role_assignments(role_id=...)`.
- Role-scoped recovery rechecks stale lease and role ownership during the update before recording a recovery event.
- `RoleService.recover_stale_assignments()` recovers only the current role's stale claims and records role-instance recovery/idle status.
- `RoleService.run_service_tick()` recovers stale claims for its role before draining queued work.
- Product Manager service coverage proves it does not recover Engineering stale claimed work.
- The implementation is a callable maintenance tick, not a background scheduler, container supervisor, or hibernation/hydration claim.
- The implementation log is factual and keeps later scheduler/process supervision scope out of this story.

### Residual Gaps

These are not blockers for PB-004 Story 6 because they remain later v2 runtime scope:

- No background scheduler invokes the maintenance tick automatically yet.
- No concurrent lease refresh while a long-running worker subprocess is blocked.
- No container hibernation/hydration behavior yet.

### Review Log

- QA-RL-006 | qa-engineer | acceptance | PB-004 Story 6 | Verified role-scoped stale recovery, service tick recovery-before-drain ordering, cross-role isolation, role-instance status reporting, and explicit non-claim of scheduler or hibernation support. | accepted 2026-06-12

## PB-004 Story 7 - Operator Stale-Recovery CLI

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
27 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
96 passed
```

### Acceptance Assessment

- `agentic-mesh-v2 recover-stale-assignments` exposes the shared stale assignment recovery path through the operator CLI.
- `--role-id` scopes recovery to one role and leaves another role's stale claimed assignment untouched.
- `--limit` is covered by regression testing and recovers only one stale assignment when invoked with `--limit 1`, leaving the other assignment claimed.
- `--reason` is recorded as the recovered assignment's audit/failure reason.
- JSON output includes `status`, `role_id`, `recovered_count`, and recovered `assignment_ids`, which is useful for operators and future schedulers.
- The implementation log is factual and does not claim this is an automatic scheduler or a real worker-adapter CLI.

### Residual Gaps

These are not blockers for PB-004 Story 7 because they remain later v2 runtime scope:

- No automatic scheduler invokes the command yet.
- No role worker is executed by this CLI command.
- No distributed locking or external process supervision is added by this story.

### Review Log

- QA-RL-007 | qa-engineer | acceptance | PB-004 Story 7 | Verified operator stale-recovery CLI, role-scoped recovery, `--limit` behavior, audit reason propagation, useful JSON output, and explicit non-claim of scheduler or worker-adapter behavior. | accepted 2026-06-12

## PB-004 Story 8 - Role-Service Tick Worker Adapter CLI

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/worker_adapters.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
30 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
99 passed
```

### Acceptance Assessment

- `SafeOutputFileWorker` is a deterministic file-backed adapter boundary for exercising role-service execution without claiming Codex/OpenAI integration.
- The adapter validates input shape, including malformed top-level file shape, non-object calls, missing `tool_name`, and non-object payloads.
- The adapter binds safe-output calls to the claimed assignment role and rejects explicit mismatched `role_id` values.
- `agentic-mesh-v2 run-role-service-tick` uses `RoleService`, processes a queued assignment, records safe outputs, terminal assignment state, and role-instance idle status.
- CLI output includes useful JSON run receipts with status, role identity, recovered count, processed count, assignment id, run id, terminal tool, and safe-output count.
- Malformed adapter output records the assignment as failed with the validation reason.
- The engineering log is factual and does not claim prompt generation, daemon supervision, or Codex/OpenAI worker integration.

### Residual Gaps

These are not blockers for PB-004 Story 8 because they remain later v2 runtime scope:

- No Codex/OpenAI worker adapter is implemented yet.
- No prompt generation or prompt audit is implemented by this story.
- No daemonized role-service supervisor is implemented by this story.

### Review Log

- QA-RL-008 | qa-engineer | acceptance | PB-004 Story 8 | Verified deterministic safe-output-file worker adapter, role binding, malformed-file validation, role-service tick CLI execution, terminal assignment recording, role-instance status, useful JSON receipts, and factual scope boundaries. | accepted 2026-06-12

## PB-004 Story 9 - Subprocess Safe-Output Worker Adapter

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/worker_adapters.py`
- `src/agentic_mesh_v2/cli.py`
- `src/agentic_mesh_v2/role_service.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
33 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
102 passed
```

### Acceptance Assessment

- `SafeOutputSubprocessWorker` runs an external command from a tuple/JSON-array command boundary and does not use shell command-string parsing.
- The adapter writes normalized assignment JSON to subprocess stdin.
- Subprocess stdout is parsed through the shared safe-output parser used by the file-backed worker.
- Parsed safe-output calls continue through the normal `RoleService` and `SafeOutputService` path, including `status.complete` terminal recording.
- Non-zero subprocess exit is recorded as an assignment failure with exit code and stderr context.
- Malformed subprocess stdout is recorded as an assignment failure with invalid-JSON context.
- CLI wiring accepts `--worker safe-output-subprocess`, `--worker-command-json`, and `--worker-timeout-seconds`.
- The engineering log is factual about scope and does not claim Codex/OpenAI prompt generation, credential management, streaming progress, or daemonized container supervision.

### Residual Gaps

These are not blockers for PB-004 Story 9 because they remain later v2 runtime scope:

- Timeout behavior is implemented and wired, but is not yet covered by a dedicated regression test.
- No Codex/OpenAI-specific prompt generation, progress streaming, credential selection, or provider failure classification is implemented by this story.
- The CLI runner still executes one bounded role-service tick rather than a daemonized role-service container.

### Review Log

- QA-RL-009 | qa-engineer | acceptance | PB-004 Story 9 | Verified subprocess worker command boundary, assignment stdin, shared safe-output parsing, normal role-service/safe-output recording, subprocess failure capture, CLI wiring, and factual scope limits. | accepted 2026-06-12

## PB-004 Story 10 - Worker Adapter Config Factory

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/worker_adapters.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_worker_adapters.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
41 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
110 passed
```

### Acceptance Assessment

- `build_worker_adapter()` creates `safe-output-file` workers from structured config with a non-empty path.
- `build_worker_adapter()` creates `safe-output-subprocess` workers from structured config with a non-empty command list and integer timeout.
- Invalid adapter names, missing file paths, missing commands, blank command items,
  non-integer timeout values, and boolean `timeout_seconds` values are rejected
  before role-service execution starts.
- `run-role-service-tick` delegates worker construction through the shared factory instead of constructing concrete worker adapters directly in the CLI command handler.
- The engineering log is factual and does not claim project YAML loading, Codex/OpenAI credential handling, prompt assembly, or daemonized supervisor behavior.

### Residual Gaps

These are not blockers for PB-004 Story 10 because they remain later v2 runtime scope:

- The factory is currently an in-process construction boundary; project YAML role-worker loading remains future work.
- Codex/OpenAI-specific worker config, credential selection, prompt assembly, and daemon supervision remain future stories.

### Review Log

- QA-RL-010 | qa-engineer | acceptance | PB-004 Story 10 | Verified shared worker-adapter config factory behavior, invalid-config rejection including boolean timeout rejection, CLI delegation through the factory, and factual scope limits. | accepted 2026-06-12

## PB-004 Story 11 - Project Worker Config Loading

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/project_config.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_project_config.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_project_config.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
50 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
119 passed
```

### Acceptance Assessment

- `load_role_worker_config()` reads `roles.<role>.worker` from project YAML.
- Invalid project config shapes, missing roles, missing requested role, missing worker config, and missing/blank adapter values are rejected.
- Relative `safe-output-file` worker paths resolve relative to the `project.yaml` directory.
- Future adapter config such as `codex-cli` is preserved by the loader.
- `run-role-service-tick --project-file` can process an assignment using project-owned `safe-output-file` worker config.
- `run-role-service-tick --project-file` with a project `codex-cli` worker config explicitly fails as unsupported, proving the runtime does not fake an adapter that has not been implemented.
- The engineering log is factual and does not claim full project schema validation, organization-default merging, or Codex/OpenAI adapter support.

### Residual Gaps

These are not blockers for PB-004 Story 11 because they remain later v2 runtime scope:

- The loader only resolves role worker config and does not validate the whole project schema.
- Organization-default and project-override merging are not implemented yet.
- `codex-cli` and other real provider adapters remain future worker-adapter stories.

### Review Log

- QA-RL-011 | qa-engineer | acceptance | PB-004 Story 11 | Verified project YAML role worker loading, invalid-shape rejection, relative path resolution, future adapter preservation with explicit unsupported-runtime rejection, CLI project-file execution, and factual scope limits. | accepted 2026-06-12

## PB-004 Story 12 - Project Role-Service Runner

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/project_config.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_project_config.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_project_config.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
54 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
123 passed
```

Additional manual verification:

```text
run-project-role-services-once with a project `codex-cli` worker and no
--skip-unsupported raised: unsupported worker adapter `codex-cli`
```

### Acceptance Assessment

- `list_project_role_service_configs()` expands configured roles and instance
  counts into stable `{project_id}.{role_id}.{index}` role instance ids.
- Invalid role instance counts are rejected.
- `run-project-role-services-once` runs one bounded `RoleService` tick per
  configured role instance.
- Multiple configured roles process their own assignments, while configured
  idle instances are still represented in role-instance status.
- Unsupported adapters such as `codex-cli` are skipped only with
  `--skip-unsupported`; without that flag they fail explicitly and are not
  faked.
- The engineering log is factual and does not claim daemonized supervision,
  organization-default merging, deployment-profile overrides, or Codex adapter
  support.

### Residual Gaps

These are not blockers for PB-004 Story 12 because they remain later v2 runtime
scope:

- The project runner runs one bounded pass and exits; long-running daemonized
  supervision is not implemented yet.
- Organization-default merging and deployment-profile worker overrides are not
  implemented yet.
- Real `codex-cli` provider execution remains a future worker-adapter story.

### Review Log

- QA-RL-012 | qa-engineer | acceptance | PB-004 Story 12 | Verified project role-service config expansion, invalid instance-count rejection, bounded project runner execution, idle instance visibility, explicit unsupported-adapter behavior, and factual scope limits. | accepted 2026-06-12

## PB-004 Story 13 - Bounded Project Role-Service Loop

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_project_config.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
```

Result:

```text
57 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
126 passed
```

### Acceptance Assessment

- `run-project-role-services-loop` runs the existing project-wide role-service
  pass for a bounded number of cycles.
- `--cycles` rejects values below 1.
- `--poll-seconds` rejects negative values.
- JSON output includes total processed, recovered, and skipped counts plus
  per-cycle receipts.
- A second cycle with no queued assignment is represented as idle/no processed
  work instead of duplicating the completed assignment.
- The engineering log is factual and does not claim daemonized supervision,
  hibernation, event wake-up, or scheduled retry support.

### Residual Gaps

These are not blockers for PB-004 Story 13 because they remain later v2 runtime
scope:

- The loop is a bounded CLI/operator entry point, not a daemonized role-service
  supervisor.
- The command sleeps between cycles but does not yet wake from connector events,
  scheduled retry timers, or hibernation/hydration lifecycle events.

### Review Log

- QA-RL-013 | qa-engineer | acceptance | PB-004 Story 13 | Verified bounded project role-service loop execution, cycle/poll validation, aggregate and per-cycle JSON receipts, idle second-cycle behavior, and factual scope limits. | accepted 2026-06-12

## PB-005 Story 1 - Hibernation Policy And Safe-Point State

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/hibernation.py`
- `src/agentic_mesh_v2/project_config.py`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_hibernation.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
57 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Result:

```text
134 passed
```

### Acceptance Assessment

- Hibernation policy supports `enabled`, `idle_after_seconds`, and
  `min_warm_instances`, with invalid scalar values rejected.
- Project hibernation defaults and role overrides load as expected.
- Safe-point evaluation refuses hibernation unless the instance is idle, has no
  current assignment, has no queued role assignment, has no claimed instance
  assignment, satisfies the idle grace period, and stays above the warm-instance
  floor.
- Durable role-instance state records `hibernating`, `hibernated`, and
  `hydrating` transitions with hibernation reason, hibernated timestamp, and
  wake reason.
- Status JSON and the dashboard role-instance table expose the hibernation
  fields.
- The engineering log is factual and explicitly does not claim container
  stop/start, event wake scheduling, process checkpointing, or full
  hibernation/hydration implementation.

### Residual Gaps

These are not blockers for PB-005 Story 1 because they remain later PB-005
scope:

- This story records logical hibernation readiness and status only; it does not
  stop or start containers.
- Event-driven wake scheduling, cooperative suspend, and full hydration remain
  future work.

### Review Log

- QA-RL-014 | qa-engineer | acceptance | PB-005 Story 1 | Verified
  hibernation policy loading and validation, safe-point refusal/approval
  behavior, durable hibernation and hydration state fields, dashboard/status
  visibility, focused regression tests, full pytest, and factual scope limits.
  | accepted 2026-06-12

## PB-005 Story 2 - Project Hibernation Maintenance Tick

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/hibernation.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_hibernation.py tests\test_v2_cli_server.py
```

Result:

```text
27 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
58 passed
```

### Acceptance Assessment

- `run-project-hibernation-maintenance` expands configured project role
  instances from `project.yaml`.
- Project and role hibernation policy is loaded before evaluating each role
  instance.
- Eligible idle instances are hibernated only after safe-point, queued-work,
  warm-floor, and grace-period checks.
- `min_warm_instances` is respected across multiple instances of the same
  role.
- Queued role work marks one hibernated instance as `hydrating` and records the
  wake reason.
- JSON output reports hibernated, hydrating, and kept-awake counts with
  per-instance reasons.
- The engineering log is factual and does not claim real container stop/start,
  event-driven wake scheduling, or full hydration.

### Residual Gaps

These are not blockers for PB-005 Story 2 because they remain later PB-005
scope:

- The maintenance command records logical hibernation and hydration state only;
  it does not stop or start containers.
- Hydration is found by a bounded maintenance scan for queued assignments, not
  by an event-driven scheduler.
- Only one hibernated instance per role is marked hydrating for queued work in
  this slice.

### Review Log

- QA-RL-015 | qa-engineer | acceptance | PB-005 Story 2 | Verified project
  hibernation maintenance expansion, policy loading, idle safe-point
  hibernation, warm-pool preservation, queued-work hydration, JSON receipts,
  focused and broader runtime tests, and factual scope limits. | accepted
  2026-06-12

## PB-005 Story 3 - Container Lifecycle Command Planning

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted after retest

### Scope Reviewed

- `src/agentic_mesh_v2/container_lifecycle.py`
- `src/agentic_mesh_v2/project_config.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_container_lifecycle.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py
```

Result:

```text
32 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
71 passed
```

### Acceptance Assessment

- Project and role `container_lifecycle` config loads with project defaults and
  role overrides.
- Docker Compose lifecycle config validates adapter, compose files, service
  name template, working directory, and exact allowed template fields.
- Regression checks reject non-string `compose_files` entries and template
  field expressions such as `{project_id[0]}` and `{project_id.__class__}`.
- Hibernated role instances plan `docker compose ... stop <service>`.
- Hydrating role instances plan `docker compose ... up -d <service>`.
- Non-actionable role states are skipped with reasons.
- `plan-project-container-lifecycle` reads durable role-instance status and
  emits JSON action/skipped counts plus command details.
- The engineering log is factual: this is dry-run planning only, with no actual
  container stop/start, Kubernetes/Helm execution, or execution result
  recording.

### Residual Gaps

These are not blockers for PB-005 Story 3 because they remain later PB-005
scope:

- The command plans Docker Compose lifecycle commands only; it does not execute
  them.
- Kubernetes, Helm, and other deployment adapters remain future slices.
- Planned lifecycle actions do not yet record execution results or update
  runtime status after container action completion.

### Review Log

- QA-RL-016 | qa-engineer | acceptance | PB-005 Story 3 | Verified dry-run
  Docker Compose lifecycle command planning, project/role config loading,
  strict compose-file and template-field validation after QA rework,
  durable-status CLI JSON receipts, focused and broader runtime tests, and
  factual scope limits. | accepted 2026-06-12

## PB-005 Story 4 - Container Lifecycle Execution Records

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted after retest

### Scope Reviewed

- `src/agentic_mesh_v2/container_lifecycle.py`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/cli.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_container_lifecycle.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py
```

Result:

```text
36 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
75 passed
```

### Acceptance Assessment

- Runtime DB records role container lifecycle attempts with command, status,
  reason, exit code, stdout, stderr, and correlation fingerprint.
- Repeated lifecycle attempts receive unique `action_id` values and preserve
  prior failure evidence.
- Status snapshot and dashboard expose role container lifecycle actions.
- The executor records planned actions without command execution.
- The executor runs through an injectable runner and finalizes the current
  attempt as succeeded or failed.
- Successful `start` returns a hydrating role instance to `idle`.
- Failed stop/start attempts preserve failure evidence and do not mark the
  role instance healthy.
- `run-project-container-lifecycle` records planned actions by default and
  executes only with `--execute`.
- The engineering log is factual about current limits: no automatic
  maintenance-triggered execution, retry, rollback, alerting, or extra stop
  success state beyond existing hibernated state.

### Residual Gaps

These are not blockers for PB-005 Story 4 because they remain later lifecycle
operations scope:

- Container execution is operator-triggered through the CLI rather than wired
  into hibernation maintenance.
- Failed lifecycle actions record evidence but do not yet trigger retry,
  rollback, or alert routing.

### Review Log

- QA-RL-017 | qa-engineer | acceptance | PB-005 Story 4 | Verified durable
  container lifecycle action records, append-only repeated-attempt evidence,
  guarded CLI execution, status/dashboard visibility, focused and broader
  runtime tests, and factual scope limits after QA rework. | accepted
  2026-06-12

## PB-005 Story 5 - Bounded Project Supervisor Tick

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_cli_server.py
```

Result:

```text
45 passed
```

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
76 passed
```

### Acceptance Assessment

- `run-project-supervisor-tick` is available as a bounded CLI command.
- The tick runs project hibernation maintenance before container lifecycle
  handling.
- Container lifecycle planning reads the durable role-instance state updated by
  the same tick.
- By default, lifecycle actions are recorded as planned and commands are not
  executed.
- `--execute` routes through the existing guarded container lifecycle executor
  path.
- JSON output contains nested hibernation and container lifecycle receipts.
- Regression coverage proves an idle surplus instance can hibernate and record
  a stop plan, then later hydrate on queued role work and record a start plan.
- The engineering log is factual: this is bounded supervisor command behavior,
  not a daemon, scheduler, retry, rollback, or alerting implementation.

### Residual Gaps

These are not blockers for PB-005 Story 5 because they remain later lifecycle
operations scope:

- The supervisor tick is operator-triggered and bounded, not a background
  daemon or scheduler.
- Retry, rollback, alert routing, and repeated automated supervision remain
  future slices.

### Review Log

- QA-RL-018 | qa-engineer | acceptance | PB-005 Story 5 | Verified bounded
  supervisor tick command behavior, hibernation-before-lifecycle ordering,
  updated durable-state lifecycle records, default planned-only mode, guarded
  `--execute` path reuse, nested JSON receipts, focused and broader runtime
  tests, and factual scope limits. | accepted 2026-06-12

## PB-005 Story 6 - Runtime Attention for Lifecycle Failures

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/container_lifecycle.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_container_lifecycle.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
40 passed
```

### Acceptance Assessment

- Failed role-container lifecycle executions create `runtime_attention_items`
  owned by `platform-engineer`, classified as `container_lifecycle_failed`,
  marked retryable, and linked to the failed lifecycle `action_id`.
- Successful lifecycle execution does not create runtime attention.
- Repeated failed attempts preserve distinct failed action records and distinct
  attention source references.
- `status_snapshot()` exposes runtime attention counts and rows.
- The v2 status dashboard renders a Runtime Attention section with reason,
  owner, source, retryability, and next-action detail.
- The engineering log accurately documents this as visibility and retry
  readiness, not automatic retry, rollback, or alert routing.

### Residual Gaps

These are not blockers for PB-005 Story 6 because they remain later runtime
operations scope:

- Runtime attention is not yet automatically closed after a later successful
  retry.
- Automatic retry policy, alert routing, and rollback execution are not part of
  this story.

### Review Log

- QA-RL-019 | qa-engineer | acceptance | PB-005 Story 6 | Verified failed
  container lifecycle executions produce actionable runtime attention linked to
  failed action evidence, successful executions produce none, repeated failures
  retain distinct evidence, status/dashboard visibility is present, focused
  tests pass, and documentation states the remaining retry/alerting limits. |
  accepted 2026-06-12

## PB-005 Story 7 - Lifecycle Attention Resolution on Success

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/container_lifecycle.py`
- `tests/test_v2_container_lifecycle.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
41 passed
```

### Acceptance Assessment

- Reviewed after the tightened retry regression was present in the workspace.
- Successful retry of the same role-container lifecycle action closes prior
  open runtime attention matched by the failed action fingerprint.
- Failed lifecycle action rows remain present and keep their failed status;
  attention closure updates only the runtime attention item, not the action
  evidence.
- The closed attention retains the failed action as `source_ref` and names the
  successful resolving action in `next_action`.
- An unrelated failed lifecycle action with a different fingerprint remains
  open after the successful retry.
- A `runtime.attention_closed` event is recorded for the resolving lifecycle
  action.

### Residual Gaps

These are not blockers for PB-005 Story 7 because they remain later runtime
operations scope:

- Attention closure depends on an identical action fingerprint; broader
  operator-driven reconciliation and scheduled retry policy are still future
  work.

### Review Log

- QA-RL-020 | qa-engineer | acceptance | PB-005 Story 7 | Verified successful
  matching lifecycle retry closes previous runtime attention, preserves failed
  action evidence, leaves unrelated failed-action attention open, records
  `runtime.attention_closed`, and passes focused runtime tests. | accepted
  2026-06-12

## PB-005 Story 8 - Operator Retry Command for Lifecycle Failures

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/container_lifecycle.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_container_lifecycle.py`
- `tests/test_v2_cli_server.py`
- `tests/test_v2_status_dashboard.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Result:

```text
44 passed
```

### Acceptance Assessment

- Reviewed after the extra negative retry coverage was present in the
  workspace.
- `retry-container-lifecycle-action` reconstructs retries from durable failed
  lifecycle action evidence instead of accepting ad hoc command input.
- The command defaults to plan-only and executes the stored command only when
  `--execute` is supplied.
- Retry planning and execution reuse the stored command, working directory,
  action reason, service, role, and role instance, preserving the action
  fingerprint needed for runtime-attention closure.
- Unknown action IDs and non-failed lifecycle actions are rejected by executor
  coverage.
- CLI JSON output includes the source failed action id, execution mode, retry
  action id, retry status, command details, working directory, and execution
  output when applicable.

### Residual Gaps

These are not blockers for PB-005 Story 8 because they remain later runtime
operations scope:

- This story adds an operator retry command, not automatic retry scheduling or
  UI action buttons.
- Execution still depends on the runtime host having a valid Docker Compose
  environment for the stored command.

### Review Log

- QA-RL-021 | qa-engineer | acceptance | PB-005 Story 8 | Verified retry
  planning/execution uses failed durable lifecycle action evidence, rejects
  unknown and non-failed action ids, preserves action fingerprints for
  attention closure, defaults to plan-only unless `--execute` is supplied, and
  passes focused runtime tests. | accepted 2026-06-12

## PB-005 Story 9 - Dashboard Retry Guidance for Runtime Attention

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_container_lifecycle.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
21 passed
44 passed
```

### Acceptance Assessment

- Open retryable `role_container_lifecycle` attention with
  `container_lifecycle_failed` now shows plan-only and `--execute` retry
  commands on the status dashboard.
- The retry commands use the failed lifecycle action's `source_ref` as the
  `--action-id` and quote both the `--db` path and `--action-id` value for
  safer copy/paste; the regression checks the HTML-escaped quoted action id.
- Closed matching lifecycle attention does not show a retry command.
- Unrelated still-open lifecycle attention continues to show its retry command.
- The server remains read-only: review found only `do_GET` routes and no HTTP
  execution endpoint or form/action path for lifecycle retries.

### Residual Gaps

These are not blockers for PB-005 Story 9 because they remain later runtime
operations scope:

- The dashboard provides copyable operator commands, not one-click execution.
- Command execution still depends on an operator environment where
  `agentic_mesh_v2.cli` can access the configured runtime database and Docker
  Compose target.

### Review Log

- QA-RL-022 | qa-engineer | acceptance | PB-005 Story 9 | Verified dashboard
  retry guidance appears only for open retryable container lifecycle failures,
  uses quoted source failed action ids, omits retry commands for closed
  attention, keeps unrelated open attention actionable, remains HTTP read-only,
  and passes focused tests. | accepted 2026-06-12

## PB-005 Story 10 - Runtime Attention Open/Closed Counts

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_container_lifecycle.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
44 passed
```

### Acceptance Assessment

- `status_snapshot()` keeps `runtime_attention_items` as the total evidence
  count and now exposes `runtime_attention_open`,
  `runtime_attention_closed`, and `runtime_attention_statuses`.
- Failed role-container lifecycle execution is covered as one open runtime
  attention item with zero closed items.
- Successful retry closure is covered as one closed matching attention item
  while an unrelated failed lifecycle action remains open.
- The status dashboard shows separate runtime attention open and closed tiles
  without removing the existing total runtime attention tile.

### Residual Gaps

None blocking for PB-005 Story 10. Dashboard filtering remains outside this
story's metric-only scope.

### Review Log

- QA-RL-023 | qa-engineer | acceptance | PB-005 Story 10 | Verified runtime
  attention total/open/closed/status metrics, dashboard open/closed tiles,
  failed-action open counts, retry-closed matching attention, preservation of
  unrelated open failures, and focused tests. | accepted 2026-06-12

## PB-005 Story 11 - Planned Lifecycle Action Idempotency

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/container_lifecycle.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py
```

Results:

```text
41 passed
```

### Acceptance Assessment

- Duplicate plan-only `run-project-container-lifecycle` calls reuse the
  existing planned action by `action_fingerprint` instead of appending another
  planned record.
- The plan-only lifecycle command reports reused actions as `already_planned`
  and increments `existing_planned_count`.
- Duplicate plan-only `retry-container-lifecycle-action` calls reuse the
  existing planned retry action by fingerprint and report `already_planned`.
- Failed and succeeded execution attempts still use append-only action records;
  execution paths continue to call `record_plan` rather than the idempotent
  plan-only helper.
- Existing lifecycle tests preserve repeated-attempt evidence for failed and
  succeeded execution records.

### Residual Gaps

None blocking for PB-005 Story 11.

### Review Log

- QA-RL-024 | qa-engineer | acceptance | PB-005 Story 11 | Verified planned
  lifecycle action idempotency for duplicate plan-only lifecycle and retry
  commands, `already_planned`/`existing_planned_count` reporting, and
  preservation of append-only failed/succeeded execution evidence. | accepted
  2026-06-12

## PB-005 Story 12 - Bounded Project Supervisor Loop

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
26 passed
83 passed
```

### Acceptance Assessment

- `run-project-supervisor-loop` validates `--cycles` must be at least 1 and
  `--poll-seconds` must be zero or greater.
- Each loop cycle delegates to the existing supervisor tick path, preserving
  hibernation maintenance and container lifecycle behavior.
- JSON output includes per-cycle receipts plus aggregate hibernation totals
  and container lifecycle totals.
- Repeated plan-only cycles reuse the existing planned lifecycle action and
  report `already_planned`/`existing_planned_count` instead of appending
  duplicate planned records.
- The command remains bounded and operator-controlled through explicit
  `--cycles`; it does not claim daemon, scheduler, or host-service behavior.

### Residual Gaps

None blocking for PB-005 Story 12.

### Review Log

- QA-RL-025 | qa-engineer | acceptance | PB-005 Story 12 | Verified bounded
  supervisor loop validation, per-cycle receipts, aggregate hibernation and
  container lifecycle totals, plan-only lifecycle idempotency across repeated
  cycles, and no daemon/scheduler claim. | accepted 2026-06-12

## PB-005 Story 13 - Dashboard Supervisor Command Guidance

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_status_dashboard.py`
- `tests/test_v2_cli_server.py`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_status_dashboard.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
31 passed
85 passed
```

### Acceptance Assessment

- `serve --project-file` is available and passes the configured project file
  through to the v2 status server.
- The v2 status dashboard renders a read-only `Supervisor Commands` section.
- When a project file is configured, the dashboard shows the configured project
  file and copyable project supervisor tick/loop commands using the same DB
  path and project file, including plan-only and `--execute` variants.
- When no project file is configured, the dashboard does not fake
  project-scoped supervisor commands and clearly says to start the status
  server with `--project-file`.
- Existing status dashboard privacy/redaction coverage remains green in the
  focused status dashboard suite.

### Residual Gaps

None blocking for PB-005 Story 13.

### Review Log

- QA-RL-026 | qa-engineer | acceptance | PB-005 Story 13 | Verified
  `serve --project-file` status-server wiring, read-only supervisor command
  dashboard guidance, plan-only and execute tick/loop command rendering with
  DB/project paths, no fake commands without a project file, and no regression
  to status privacy/redaction tests. | accepted 2026-06-12

## PB-005 Story 14 - Project Supervisor Service Entrypoint

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `src/agentic_mesh_v2/server.py`
- `tests/test_v2_cli_server.py`
- `tests/test_v2_status_dashboard.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
36 passed
```

### Acceptance Assessment

- `run-project-supervisor-service` is registered in the v2 CLI.
- The service command requires an explicit mode through mutually exclusive
  `--cycles N` or `--continuous`.
- Bounded service execution delegates to the existing supervisor tick path,
  aggregates hibernation and container lifecycle totals, and preserves
  plan-only lifecycle idempotency across repeated cycles.
- Continuous service interruption returns an `interrupted` receipt, with
  `cycles_completed` counting only completed cycles.
- The status dashboard includes the continuous service command only when the
  status server has a configured project file.
- The engineering log limits the claim to the service entrypoint and does not
  claim host deployment wiring, Compose/systemd/K8s supervisor wiring, or
  automatic scheduling.

### Residual Gaps

None blocking for PB-005 Story 14.

### Review Log

- QA-RL-027 | qa-engineer | acceptance | PB-005 Story 14 | Verified explicit
  supervisor service CLI mode selection, bounded cycle aggregation,
  lifecycle idempotency, clean interrupt receipts, dashboard command scoping,
  and factual engineering-log boundaries. | accepted 2026-06-12

## PB-005 Story 15 - Dogfood Compose Supervisor Service Wiring

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml`
- `examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml`
- `tests/test_v2_dogfood_compose.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_dogfood_compose.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
docker compose -f examples\projects\agentic-mesh-dev\deploy\compose\docker-compose.yml -f examples\projects\agentic-mesh-dev\deploy\compose\docker-compose.linuxch.yml config --quiet
```

Results:

```text
39 passed
docker compose config passed
```

### Acceptance Assessment

- `v2-runtime` starts the status server with
  `serve --project-file /mesh/project/agentic-mesh/project.yaml`.
- The dogfood Compose file defines `v2-supervisor` using the shared
  Agentic Mesh image, environment, volumes, and working directory.
- `v2-supervisor` runs
  `run-project-supervisor-service --continuous --execute` with the configured
  project file and poll interval.
- The linuxch overlay gives `v2-supervisor` a restart policy.
- Compose validation passes with the base file and linuxch overlay.
- Test and implementation claims stay scoped to the dogfood Docker Compose
  supervisor container and do not claim systemd, Kubernetes, Helm, Terraform,
  or role worker service deployment support.

### Residual Gaps

None blocking for PB-005 Story 15.

### Review Log

- QA-RL-028 | qa-engineer | acceptance | PB-005 Story 15 | Verified dogfood
  Compose status-server project-file wiring, continuous executing supervisor
  service wiring, linuxch restart policy, Compose config validity, and scoped
  deployment claims. | accepted 2026-06-12

## PB-005 Story 16 - Single Role Service Entrypoint

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py
```

Results:

```text
36 passed
```

### Acceptance Assessment

- `run-role-service-loop` is registered in the v2 CLI.
- The command targets one explicit role id and role-instance id, which supports
  one-container-per-role-instance deployment wiring.
- The command requires explicit bounded or continuous service mode.
- Bounded execution reuses the existing role-service tick path and aggregates
  recovered/processed totals across cycles.
- Continuous interruption returns an `interrupted` receipt and counts only
  completed cycles.
- The implementation log limits the claim to the entrypoint and does not claim
  dogfood Compose role-container deployment or Codex-worker execution support.

### Residual Gaps

None blocking for PB-005 Story 16.

### Review Log

- QA-RL-029 | qa-engineer | acceptance | PB-005 Story 16 | Verified
  single-role service loop CLI registration, explicit role-instance targeting,
  explicit mode selection, bounded aggregate receipts, clean interrupt
  receipts, and scoped implementation claims. | accepted 2026-06-12

## PB-005 Story 17 - Command-Backed Codex CLI Worker Adapter

Date: 2026-06-12

QA role: QA Engineer

Decision: rework required

### Scope Reviewed

- `src/agentic_mesh_v2/worker_adapters.py`
- `src/agentic_mesh_v2/cli.py`
- `config/schemas/project.schema.json`
- `tests/test_v2_worker_adapters.py`
- `tests/test_v2_cli_server.py`
- `tests/test_v2_project_config.py`
- `tests/test_v2_project_schema.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_project_config.py tests\test_v2_project_schema.py
```

Results:

```text
61 passed
```

Additional QA probe:

```text
python - << equivalent inline check of agentic_mesh_v2.cli._worker_config_from_args for --worker codex-cli defaults
```

Result:

```text
{'adapter': 'codex-cli', 'timeout_seconds': 300}
```

### Acceptance Assessment

- `adapter: codex-cli` is implemented as a command-backed worker adapter.
- The adapter sends assignment and worker metadata to stdin and parses
  safe-output JSON from stdout.
- The adapter rejects invalid command, timeout, reasoning effort, sandbox mode,
  and auth mapping configuration.
- The adapter-level default command is explicit as `codex exec`, and the
  adapter-level default timeout is four hours.
- CLI role-service tick/loop commands accept `--worker codex-cli`.
- Project-file role-service execution can run a configured `codex-cli` command
  in tests.
- Unknown future adapters remain explicit/skippable.
- The project schema allows command-backed worker fields.
- The engineering log correctly states that prompt assembly and real
  safe-output tool transport remain future work.

### Required Rework

- The CLI path for `--worker codex-cli` currently injects
  `timeout_seconds: 300` when no explicit timeout is provided, overriding the
  adapter's four-hour Codex default. This violates the Story 17 requirement
  that default Codex command construction use a long default timeout suitable
  for Codex runs. Change the CLI defaulting behavior so `codex-cli` uses the
  long default unless the operator explicitly supplies
  `--worker-timeout-seconds`, and add a regression test for that path.

### Review Log

- QA-RL-030 | qa-engineer | acceptance | PB-005 Story 17 | Focused tests pass,
  and most adapter/schema/CLI/project-file behavior is covered, but the direct
  CLI `--worker codex-cli` path overrides the long Codex timeout with 300
  seconds. | rework required 2026-06-12

## PB-005 Story 17 - Command-Backed Codex CLI Worker Adapter Retest

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted after rework

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `src/agentic_mesh_v2/worker_adapters.py`
- `tests/test_v2_cli_server.py`
- `tests/test_v2_worker_adapters.py`
- `tests/test_v2_project_config.py`
- `tests/test_v2_project_schema.py`

### Commands Run

Direct timeout probe:

```text
python - << equivalent inline check of agentic_mesh_v2.cli._worker_config_from_args and build_worker_adapter for codex-cli and safe-output-subprocess timeout defaults
```

Results:

```text
codex_default_cli_config {'adapter': 'codex-cli'}
codex_explicit_cli_config {'adapter': 'codex-cli', 'timeout_seconds': 900}
subprocess_default_cli_config {'adapter': 'safe-output-subprocess', 'command': ['C:\\Python314\\python.exe', '-c', "print('{}')"]}
codex_default_adapter_timeout 14400
codex_explicit_adapter_timeout 900
subprocess_default_adapter_timeout 300
```

Focused regression suite:

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_project_config.py tests\test_v2_project_schema.py
```

Results:

```text
64 passed
```

### Acceptance Assessment

- Direct CLI `--worker codex-cli` no longer injects `timeout_seconds` when
  `--worker-timeout-seconds` is omitted.
- The adapter default of 14400 seconds now applies for direct CLI
  `--worker codex-cli` when no explicit timeout is supplied.
- Direct CLI `--worker codex-cli --worker-timeout-seconds N` still passes the
  explicit timeout through to the adapter.
- Direct CLI `safe-output-subprocess` also preserves its adapter default when
  no timeout is supplied.
- Focused tests pass.

### Residual Gaps

None blocking for PB-005 Story 17.

### Review Log

- QA-RL-031 | qa-engineer | retest | PB-005 Story 17 | Verified the CLI
  timeout default regression is closed for Codex CLI and preserved for
  safe-output subprocess, with focused tests passing. | accepted 2026-06-12

## PB-005 Story 18 - Dogfood Role-Agent Service Deployment Wiring

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml`
- `examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml`
- `examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml`
- `config/schemas/project.schema.json`
- `tests/test_v2_dogfood_compose.py`
- `tests/test_v2_project_schema.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
git status --short --branch
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_dogfood_compose.py tests\test_v2_project_schema.py
docker compose -f examples\projects\agentic-mesh-dev\deploy\compose\docker-compose.yml -f examples\projects\agentic-mesh-dev\deploy\compose\docker-compose.linuxch.yml config --quiet
```

Results:

```text
7 passed
Compose config validation passed
Read-only YAML comparison found 15 expected role instances, 15 role services,
no missing role services, no extra role-loop services, and no missing linuxch
restart entries.
```

### Acceptance Assessment

- Dogfood project lifecycle config uses
  `{project_id}-{role_id}-{index}`.
- Dogfood Compose defines one `run-role-service-loop` service for every
  configured role instance, including both engineering instances.
- Role services run continuous mode with explicit role and instance identity.
- Linuxch overlay restarts every role service.
- Tests expand the project role config and lifecycle template rather than only
  asserting fixed service text.
- Engineering evidence correctly does not claim XML prompt assembly or native
  safe-output transport is complete.

### Residual Risks

- Linuxch overlay restart coverage includes a formatting-sensitive text
  assertion. The Compose semantic validation and project-expanded service tests
  are sufficient for this story, but future Compose-generation work should move
  more of this into structured validation.

### Review Log

- QA-RL-032 | qa-engineer | acceptance | PB-005 Story 18 | Verified dogfood
  role-agent service deployment wiring, lifecycle service-name alignment,
  continuous role-service commands, linuxch restart policy, and scoped
  implementation claims. | accepted 2026-06-12

## PB-005 Story 19 - XML Worker Prompt Assembly And Audit

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `config/prompts/worker/system-security.xml`
- `config/prompts/worker/safe-outputs.xml`
- `config/prompts/worker/instructions.xml`
- `src/agentic_mesh_v2/prompt_builder.py`
- `src/agentic_mesh_v2/role_service.py`
- `src/agentic_mesh_v2/worker_adapters.py`
- `src/agentic_mesh_v2/db.py`
- `src/agentic_mesh_v2/cli.py`
- `tests/test_v2_prompt_builder.py`
- `tests/test_v2_role_assignment_execution.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
git status --short --branch
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_prompt_builder.py tests\test_v2_role_assignment_execution.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests
$env:PYTHONPYCACHEPREFIX=<temp>; python -m compileall -q src\agentic_mesh_v2
```

Results:

```text
77 passed focused suite
193 passed full suite
compileall passed
```

### Acceptance Assessment

- Prompt assembly is config/data driven: prompt component files, role YAML,
  project YAML, assignment fields, flow hints, conversation context, memory
  context, and safe-output tool policy are rendered together.
- Prompt XML includes `system-security`, `safe-outputs`, `role`, `project`,
  `assignment`, `current-flow-state`, `conversation-context`, `memory-context`,
  and `instructions`.
- Full prompt audit is recorded before worker execution and exposed through
  runtime state.
- Prompt assembly failure completes the run and assignment as failed rather
  than leaving stale running state.
- `RoleAssignment.generated_prompt` reaches the worker, and `CodexCliWorker`
  sends it as top-level stdin `prompt`.
- Engineering evidence correctly does not claim native MCP/safe-output tool
  transport is complete.

### Residual Risks

- Prompt quality is intentionally first-cut text and needs future
  prompt-engineering refinement.
- Native MCP/safe-output tool transport is still not implemented; workers must
  continue emitting safe-output JSON on stdout.

### Review Log

- QA-RL-033 | qa-engineer | acceptance | PB-005 Story 19 | Verified XML prompt
  assembly, full prompt audit, worker prompt injection, Codex CLI prompt
  payload, prompt-failure runtime recovery, and scoped implementation claims. |
  accepted 2026-06-12

## PB-005 Story 20 - Safe-Output CLI Transport Command

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/cli.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_cli_server.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_safe_outputs.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests
python -m compileall -q src\agentic_mesh_v2
```

Results:

```text
47 passed at QA review
198 passed full suite at QA review
compileall passed
50 passed focused suite after engineering added residual branch tests
```

### Acceptance Assessment

- `record-safe-output` records through `SafeOutputService` and does not bypass
  policy validation.
- The command validates run existence, running status, and role ownership before
  recording.
- It rejects invalid JSON, non-object payloads, unauthorized tools, fake durable
  mutation claims, unknown runs, and non-running runs.
- Success and failure responses are structured JSON receipts.
- DB row conversion now exposes `terminal` as a real boolean.
- Engineering evidence correctly does not claim Codex worker collection of
  CLI-recorded calls or MCP support.

### Residual Risks

- This story provides the CLI front door only. `CodexCliWorker` still needs a
  follow-up slice to collect safe-output calls recorded during a subprocess run,
  and MCP exposure remains a later slice.

### Review Log

- QA-RL-034 | qa-engineer | acceptance | PB-005 Story 20 | Verified safe-output
  CLI recording, ownership validation, shared policy enforcement, structured
  receipts, boolean decoding, and scoped implementation claims. | accepted
  2026-06-12

## PB-005 Story 21 - Collect CLI-Recorded Safe Outputs From Worker Runs

Date: 2026-06-12

QA role: QA Engineer

Decision: accepted

### Scope Reviewed

- `src/agentic_mesh_v2/role_service.py`
- `src/agentic_mesh_v2/worker_adapters.py`
- `src/agentic_mesh_v2/db.py`
- `tests/test_v2_role_assignment_execution.py`
- `tests/test_v2_worker_adapters.py`
- `docs/engineering/v2-role-execution-implementation-log.md`

### Commands Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_safe_outputs.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests
python -m compileall -q src\agentic_mesh_v2
```

Results:

```text
89 passed focused suite
203 passed full suite
compileall passed
```

### Acceptance Assessment

- Role services inject `run_id` and CLI `safe_output_transport` before prompt
  rendering and worker execution.
- Legacy stdout-returned calls are recorded first; then run-scoped DB calls are
  collected and checked for role ownership and terminal output.
- A worker can complete by recording `status.complete` through the CLI transport
  and returning no safe-output JSON.
- `CodexCliWorker` sends top-level `safe_outputs` and tolerates non-JSON stdout
  only when transport is present.
- `SafeOutputSubprocessWorker` allows empty stdout with transport but still
  rejects invalid non-empty JSON.
- Engineering evidence does not overclaim MCP or connector delivery side
  effects.

### Residual Risks

- QA noted terminal selection could be ambiguous if multiple terminal calls
  shared a timestamp second. Engineering changed run-scoped safe-output
  collection to order by SQLite insertion row after QA review, preserving insert
  order for this local SQLite runtime.

### Review Log

- QA-RL-035 | qa-engineer | acceptance | PB-005 Story 21 | Verified
  CLI-recorded safe-output collection, run-bound transport injection, Codex
  transport payload, subprocess strictness, and scoped implementation claims. |
  accepted 2026-06-12
