# V2 Role Execution Implementation Log

Status: in progress

Owner role: engineering

Date started: 2026-06-12

Source documents:

- `docs/product/backlog.md`
- `docs/architecture/v2-runtime-reset.md`
- `docs/architecture/agentic-mesh-design.md`

## PB-004 Story 1 - Claim And Run Role Assignments

Status: implemented, QA accepted.

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

Status: implemented, QA accepted.

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

## PB-004 Story 3 - Terminal Assignment Outcome States

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Map terminal safe-output tools to visible role-assignment outcomes instead of
treating every terminal call as completed. This prevents blocked work, human
waits, incomplete runs, and no-op outcomes from being hidden behind a generic
completed state.

### Implementation Notes

- Added `noop` as a first-class common terminal safe-output tool with required
  `reason`.
- Extended role assignment completion so callers can set the terminal status.
- Added role-service terminal mapping:
  - `status.reply`, `status.complete`, `handoff.request`, `consult.request`,
    `queue.propose_item`, `release.close`, and `noop` -> `completed`
  - `sponsor.ask_question`, `human_response.request`, and
    `release.request_approval` -> `waiting_human`
  - `report.blocked` -> `blocked`
  - `report.incomplete` -> `incomplete`
- Status snapshots already expose `role_assignment_statuses`, so these outcomes
  are visible without reading safe-output records manually.

### Tests Added

- `report.blocked` marks a role assignment `blocked`.
- `sponsor.ask_question` marks a role assignment `waiting_human`.
- `report.incomplete` marks a role assignment `incomplete`.
- `noop` marks a role assignment `completed` with terminal tool `noop`.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_safe_outputs.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
13 passed
85 passed
```

### Known Limitations

- Waiting-human assignments are visible in runtime state but are not yet linked
  to an automatic scheduler/retry loop.
- Blocked and incomplete assignments are not yet automatically recovered or
  escalated beyond their recorded terminal state.
- Continuous role-service loops, leases, heartbeat refresh, and hibernation
  remain later PB-004/PB-005 slices.

## PB-004 Story 4 - Bounded Role-Service Drain And Heartbeat

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add the first long-running role-service shape without introducing container
hibernation or lease recovery yet. A role service can drain a bounded number of
queued assignments for its role, update durable role-instance status, and expose
heartbeat/last-run data to the dashboard.

### Implementation Notes

- Added `role_instance_status` runtime table.
- Added `V2Database.update_role_instance_status()` and
  `list_role_instance_statuses()`.
- `RoleService` now records an idle status when initialized.
- `run_next_assignment()` records:
  - `idle` when no work is available
  - `active` while processing a claimed assignment
  - `failed` when worker/safe-output execution fails
  - `idle` after a terminal assignment outcome is recorded
- Added `RoleService.drain_available_assignments(max_assignments=...)` for a
  bounded role-service loop.
- Status snapshots expose `role_instance_statuses`.
- The status dashboard renders role instance status, current assignment, last
  run, processed count, heartbeat time, and detail.

### Tests Added

- role service drains two queued assignments and records idle heartbeat/status
- drain respects `max_assignments`
- dashboard renders role instance status and last-run details

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
16 passed
88 passed
```

### Known Limitations

- This is a bounded in-process drain loop, not a container supervisor.
- There is no claim lease timeout, stale-claim recovery scan, or cooperative
  hibernation/hydration yet.
- `processed_count` records the latest drain batch size rather than a lifetime
  metric.

## PB-004 Story 5 - Assignment Lease And Stale Claim Recovery

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add lease metadata and stale-claim recovery for role assignments so claimed work
does not remain invisible forever if a role service exits, loses credits, or
otherwise stops before recording a terminal safe-output result.

### Implementation Notes

- Added role-assignment lease/recovery metadata:
  - `claim_expires_at`
  - `recovery_count`
- `claim_role_assignment()` now sets a configurable lease expiry when a queued
  assignment is claimed.
- Added `refresh_role_assignment_lease()` and
  `RoleService.refresh_assignment_lease()` for cooperative long-running role
  services.
- `complete_role_assignment()` and `fail_role_assignment()` clear the active
  claim lease when an assignment reaches a terminal state.
- Added `recover_stale_role_assignments()` to return expired claimed
  assignments to `queued`, clear role-instance ownership, preserve a recovery
  reason, increment `recovery_count`, and append a recovery event.
- Reclaiming recovered queued work clears stale terminal/failure fields while
  preserving the recovery count and event audit trail.
- The status dashboard now renders lease expiry and recovery count for role
  assignments.

### Tests Added

- claiming an assignment sets lease metadata and terminal completion clears it
- stale claimed assignments recover back to `queued` with recovery reason,
  count, and event
- recovered assignments clear stale terminal/failure fields when reclaimed
- unexpired claimed assignments are not recovered
- dashboard output includes lease/recovery visibility

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
20 passed
92 passed
```

### Known Limitations

- This story adds the recovery operation but not an automatic scheduler or
  periodic recovery poller.
- Lease refresh is explicit; continuous heartbeat-driven refresh remains a
  later service-loop/supervisor slice.
- This is assignment recovery only. Container hibernation/hydration remains
  PB-005.

## PB-004 Story 6 - Role-Service Maintenance Tick

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a bounded role-service maintenance tick that recovers stale claimed
assignments for the service's own role before claiming and processing available
work. This moves PB-004 from manual recovery operations toward an inspectable
service loop without claiming container supervision or hibernation.

### Implementation Notes

- Extended `recover_stale_role_assignments()` with optional `role_id` scoping.
- Role-scoped recovery rechecks the stale lease condition during the update
  before recording the recovery event.
- Added `RoleService.recover_stale_assignments()` for role-owned recovery with
  role-instance status updates.
- Added `RoleService.run_service_tick()` to:
  - recover stale assignments for the current role
  - drain a bounded batch of queued assignments
  - record a final idle status with recovered and processed counts
- The maintenance tick does not recover other roles' stale assignments.

### Tests Added

- stale assignment recovery can be scoped to one role
- a role-service maintenance tick recovers a stale assignment for its role and
  then processes it
- a role-service maintenance tick leaves other roles' stale claims untouched

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
22 passed
94 passed
```

### Known Limitations

- This is a callable service-loop tick, not a background scheduler process.
- Lease refresh still happens through explicit service calls; there is no
  concurrent refresh while a long-running worker subprocess is blocked.
- Container hibernation/hydration and process supervision remain PB-005.

## PB-004 Story 7 - Operator Stale-Recovery CLI

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Expose stale assignment recovery through the v2 CLI so operators, scripts, and
future supervisors can recover expired claimed assignments without writing
ad-hoc database mutations. This story deliberately avoids adding a fake CLI role
worker until the real worker adapter is available.

### Implementation Notes

- Added `recover-stale-assignments` to `agentic-mesh-v2`.
- The command accepts:
  - `--role-id` for role-scoped recovery
  - `--limit` for bounded recovery batches
  - `--reason` for auditable recovery notes
- The command uses the same database recovery path as role-service maintenance
  ticks.
- JSON output includes status, role id, recovered count, and recovered
  assignment ids.

### Tests Added

- CLI stale recovery recovers only the requested role's expired claims.
- CLI stale recovery records the supplied audit reason and increments recovery
  count.
- CLI stale recovery does not recover another role's stale claim when `--role-id`
  is supplied.
- CLI stale recovery respects `--limit` for bounded recovery batches.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
27 passed
96 passed
```

### Known Limitations

- This command is an operator/supervisor entry point, not an automatic scheduler.
- The command does not execute role workers; real worker-adapter CLI/supervisor
  integration remains a later PB-004/PB-005 slice.

## PB-004 Story 8 - Role-Service Tick Worker Adapter CLI

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add the first explicit worker-adapter boundary for running a role-service tick
from the CLI. The adapter is deterministic and file-backed so local operators,
tests, and future supervisors can exercise role claiming, safe-output recording,
and terminal assignment handling through the same `RoleService` path without
pretending Codex CLI integration is complete.

### Implementation Notes

- Added `agentic_mesh_v2.worker_adapters.SafeOutputFileWorker`.
- The file adapter reads either a JSON list of safe-output calls or an object
  with a `calls` list.
- The adapter binds calls to the claimed assignment role and rejects explicit
  mismatched `role_id` values.
- Added `run-role-service-tick` to the v2 CLI with:
  - `--role-id`
  - `--role-instance-id`
  - `--worker safe-output-file`
  - `--safe-output-file`
  - `--max-recoveries`
  - `--max-assignments`
  - `--assignment-lease-seconds`
- CLI JSON output reports recovered count, processed count, and run receipts.

### Tests Added

- CLI role-service tick processes a queued assignment from a safe-output file.
- CLI role-service tick records terminal assignment state and role-instance
  idle status.
- CLI role-service tick rejects a safe-output file that emits for another role
  and records the assignment failure reason.
- CLI role-service tick rejects malformed safe-output files and records the
  assignment failure reason.

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
30 passed
99 passed
```

### Known Limitations

- `safe-output-file` is a deterministic local/operator adapter, not the final
  Codex/OpenAI worker adapter.
- The CLI runner runs one bounded tick and exits; daemonized container service
  supervision remains later PB-004/PB-005 work.
- The adapter does not generate prompts or invoke models.

## PB-004 Story 9 - Subprocess Safe-Output Worker Adapter

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a subprocess worker-adapter boundary that can run an external command,
provide assignment context on stdin, parse safe-output JSON from stdout, and
record failures through the existing role-service path. This is the immediate
predecessor to a Codex/OpenAI adapter without hard-coding a model provider into
the runtime.

### Implementation Notes

- Added `SafeOutputSubprocessWorker`.
- The subprocess adapter:
  - accepts a command tuple and timeout
  - writes normalized assignment JSON to stdin
  - captures stdout and stderr
  - parses stdout using the shared safe-output parser
  - raises clear timeout, non-zero exit, missing stdout, and invalid JSON errors
- Shared safe-output JSON parsing now serves both file and subprocess workers.
- `run-role-service-tick` now accepts `--worker safe-output-subprocess`,
  `--worker-command-json`, and `--worker-timeout-seconds`.
- CLI subprocess command configuration uses a JSON command array, avoiding shell
  command-string parsing and quoting hazards.

### Tests Added

- subprocess worker receives assignment JSON and completes a role assignment
  through `status.complete`
- subprocess worker safe-output payload is recorded through the normal
  safe-output service
- non-zero subprocess exit marks the assignment failed with exit code and stderr
  context
- invalid subprocess stdout JSON marks the assignment failed with a useful
  parse error

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
33 passed
102 passed
```

### Known Limitations

- This adapter executes an external process but does not yet generate prompts,
  stream progress, classify provider-specific failures, or manage credentials.
- Codex/OpenAI-specific worker behavior remains later adapter work.
- The CLI runner still runs one bounded tick and exits rather than acting as a
  daemonized role-service container.

## PB-004 Story 10 - Worker Adapter Config Factory

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a small worker-adapter construction boundary so CLI, future project config,
and future supervisor code can build worker adapters from validated structured
configuration instead of duplicating adapter-specific construction logic.

### Implementation Notes

- Added `build_worker_adapter()`.
- The factory supports:
  - `safe-output-file` with a non-empty path
  - `safe-output-subprocess` with a non-empty command list and integer timeout
- `run-role-service-tick` now converts CLI arguments into a config dictionary
  and delegates adapter construction to the shared factory.
- Invalid adapter config fails before a role service is started.

### Tests Added

- worker adapter factory creates and runs a configured `safe-output-file`
  adapter
- worker adapter factory creates a configured `safe-output-subprocess` adapter
- invalid adapter names, missing paths, missing commands, blank command items,
  non-integer timeouts, and boolean timeouts are rejected
- existing CLI role-service tick tests continue to prove CLI wiring through the
  factory path

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
41 passed
110 passed
```

### Known Limitations

- The factory is an in-process config boundary; it does not yet load project
  YAML role worker settings.
- Codex/OpenAI-specific adapter config, credentials, and prompt assembly remain
  later worker-adapter stories.

## PB-004 Story 11 - Project Worker Config Loading

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Load `roles.<role>.worker` from project YAML so role-service ticks can use
project-owned worker configuration instead of relying only on CLI adapter flags.
This keeps the runtime moving toward project-scoped role services while leaving
unsupported future adapters, such as `codex-cli`, explicit rather than faked.

### Implementation Notes

- Added `load_role_worker_config()`.
- The loader validates:
  - project config is a mapping
  - `roles` is present
  - requested role exists
  - requested role has a worker config
  - worker config has a non-empty adapter
- `safe-output-file` worker paths are resolved relative to the project YAML
  directory when they are not absolute.
- Future adapter config, such as `codex-cli`, is preserved by the loader but
  remains unsupported by `build_worker_adapter()` until the adapter exists.
- `run-role-service-tick` now accepts `--project-file`; when `--worker` is
  omitted it loads the worker config for `--role-id` from that project file.

### Tests Added

- project worker config loader resolves relative `safe-output-file` paths
- project worker config loader preserves future `codex-cli` worker settings
- invalid project config shapes and missing role/worker/adapter data are
  rejected
- CLI role-service tick can process an assignment using worker config loaded
  from project YAML
- CLI role-service tick rejects project `codex-cli` config until that adapter
  is implemented

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_project_config.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
50 passed
119 passed
```

### Known Limitations

- The loader only reads role worker config; it does not yet validate the entire
  project schema or merge organization defaults.
- Unsupported adapters such as `codex-cli` are preserved but still rejected by
  the worker factory until their concrete adapter is implemented.

## PB-004 Story 12 - Project Role-Service Runner

Status: implemented, awaiting QA review.

Owner role: Engineering

Date: 2026-06-12

### Scope

Run one bounded role-service tick for every configured role instance in a
project file. This turns project-owned role/instance/worker config into actual
runtime role-service execution without requiring operators to invoke each role
instance manually.

### Implementation Notes

- Added `ProjectRoleServiceConfig`.
- Added `list_project_role_service_configs()` to expand project roles and
  instance counts into concrete role-service instance configs.
- Role instance ids use `{project_id}.{role_id}.{index}`.
- Added `run-project-role-services-once` CLI command.
- The command runs one bounded `RoleService.run_service_tick()` per configured
  role instance.
- JSON output includes project file, recovered/processed/skipped totals, and
  per-role-instance receipts.
- Added `--skip-unsupported` so future adapters such as `codex-cli` can be
  reported as skipped instead of faked.

### Tests Added

- project role-service configs expand multi-instance roles into stable instance
  ids
- invalid role instance counts are rejected
- project runner processes assignments for multiple configured roles
- project runner records idle status for configured role instances, including
  idle instances with no work
- project runner can skip unsupported adapters while still processing supported
  roles

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_project_config.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
54 passed
123 passed
```

### Known Limitations

- The command runs one bounded pass and exits; it is not a daemonized
  supervisor.
- Unsupported adapters are skipped only when explicitly requested by the
  operator.
- The project runner does not yet merge organization defaults or deployment
  profile overrides.

## PB-004 Story 13 - Bounded Project Role-Service Loop

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add an explicit bounded loop command for project role services so local
operators and future container entrypoints can run repeated project-wide
role-service ticks without introducing an uncontrolled daemon.

### Implementation Notes

- Added `run-project-role-services-loop`.
- The loop command accepts:
  - `--project-file`
  - `--cycles`
  - `--poll-seconds`
  - `--max-recoveries`
  - `--max-assignments`
  - `--assignment-lease-seconds`
  - `--skip-unsupported`
- The command runs the existing project-wide role-service pass once per cycle.
- JSON output includes total processed/recovered/skipped counts and per-cycle
  results.
- Loop bounds reject zero/negative cycles and negative polling intervals.

### Tests Added

- bounded loop with two cycles processes work in the first cycle and reports an
  idle second cycle
- completed assignment state is preserved after the loop
- invalid cycle count and negative poll interval are rejected

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_project_config.py tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_role_assignment_execution.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider
```

Results:

```text
57 passed
126 passed
```

### Known Limitations

- This is bounded loop execution, not an always-on supervisor or hibernating
  container manager.
- The command sleeps between cycles but does not yet wake from external events,
  connector notifications, or scheduled retry timers.

## PB-005 Story 1 - Hibernation Policy And Safe-Point State

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add the first durable hibernation readiness slice for v2 role instances. This
story defines configurable hibernation policy, safe-point evaluation, durable
hibernation/hydration status recording, and dashboard visibility. It does not
stop or start containers.

### Implementation Notes

- Added `HibernationPolicy` with:
  - `enabled`
  - `idle_after_seconds`
  - `min_warm_instances`
- Added project-level and role-level hibernation config loading. Role config
  overrides project defaults.
- Added `HibernationService.evaluate()` to allow hibernation only when:
  - the policy is enabled
  - the role instance exists and belongs to the requested role
  - the role instance is `idle`
  - there is no current assignment
  - there is no queued work for the role
  - there is no claimed work for the instance
  - hibernating would not violate the configured warm-instance floor
  - the idle grace period has elapsed
- Added durable role-instance fields:
  - `hibernation_reason`
  - `hibernated_at`
  - `wake_reason`
- Added durable hibernation state updates for `hibernating`, `hibernated`, and
  `hydrating`.
- Updated the status dashboard role-instance table to show hibernation and wake
  evidence.

### Tests Added

- project hibernation defaults and role overrides
- invalid hibernation policy values
- idle safe-point hibernation after the grace period
- refusal while active
- refusal when queued work exists for the role
- warm-instance floor protection
- hydration records wake reason while preserving hibernation reason
- dashboard renders hibernation evidence

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_hibernation.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
8 passed
57 passed
```

### Known Limitations

- This story records logical hibernation readiness only. It does not stop,
  start, pause, or resume containers.
- There is no event-driven wake scheduler yet.
- Worker subprocess checkpointing and cooperative suspend remain later PB-005
  work.

## PB-005 Story 2 - Project Hibernation Maintenance Tick

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a bounded project hibernation maintenance command so an operator or future
supervisor can apply hibernation policy across configured project role
instances. The command records logical hibernation and hydration state; it does
not stop or start containers.

### Implementation Notes

- Added `run-project-hibernation-maintenance`.
- The command expands configured project role instances from `project.yaml`.
- For each role, the command loads project/role hibernation policy.
- The command marks eligible idle role instances `hibernated` after the safe
  point and grace-period checks pass.
- The command respects `min_warm_instances` while hibernating multiple
  instances of the same role.
- If queued work exists for a role, the command marks one hibernated instance
  `hydrating` and records the wake reason.
- JSON output includes hibernated, hydrating, and kept-awake counts plus
  per-instance reasons.

### Tests Added

- project hibernation maintenance hibernates only surplus idle instances when a
  warm-instance floor is configured
- queued role work hydrates a hibernated instance
- maintenance output reports hibernated, hydrating, and kept-awake counts
- wake reason is durable after hydration

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_hibernation.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
27 passed
58 passed
```

### Known Limitations

- This story still records logical runtime state only. Actual container
  stop/start integration remains a later PB-005 slice.
- The hydration trigger is a bounded maintenance scan for queued assignments,
  not a connector event listener or scheduler.
- Only one hibernated role instance is marked hydrating for queued work in this
  first maintenance slice.

## PB-005 Story 3 - Container Lifecycle Command Planning

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a dry-run container lifecycle planner that maps v2 role-instance
hibernation state to explicit Docker Compose commands. This story gives the
future supervisor and Release/Platform roles an auditable command plan before
actual container stop/start execution is enabled.

### Implementation Notes

- Added `container_lifecycle` project/role config loading.
- Supported v1 adapter is `docker-compose`.
- Added `ComposeRoleLifecycleConfig` with:
  - `compose_files`
  - `service_name_template`
  - `working_directory`
- Service name templates support:
  - `{project_id}`
  - `{role_id}`
  - `{role_instance_id}`
  - `{index}`
- Added lifecycle command planning:
  - `hibernated` role instances plan `docker compose ... stop <service>`
  - `hydrating` role instances plan `docker compose ... up -d <service>`
  - other role-instance states are skipped with reasons
- Added `plan-project-container-lifecycle` CLI command.
- The CLI emits JSON with action count, skipped count, per-action commands, and
  skipped reasons. It does not execute the commands.

### Tests Added

- project defaults and role overrides for container lifecycle config
- invalid lifecycle config rejection
- Docker Compose stop/start command generation
- numeric role-instance index validation for `{index}`
- CLI planning from durable hibernated/hydrating role-instance state

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
32 passed
71 passed
```

### QA Rework

- Tightened `compose_files` loading so non-string project YAML entries are
  rejected instead of coerced to strings.
- Tightened service-name template validation so only exact allowed field names
  are accepted; indexing and attribute expressions such as `{project_id[0]}` or
  `{project_id.__class__}` are rejected.
- Added regression coverage for both validation gaps.

### Known Limitations

- This story plans container commands only; it does not execute them.
- Only Docker Compose command planning is implemented. Kubernetes, Helm, and
  other enterprise deployment adapters remain future slices.
- The planner maps current runtime state to commands but does not yet record
  command execution results or update status after container action completion.

## PB-005 Story 4 - Container Lifecycle Execution Records

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add durable execution records for role container lifecycle actions and a guarded
CLI path that can either record planned actions or execute them through an
injectable command runner. This is the first slice that records container
lifecycle outcomes, while keeping real command execution behind an explicit
operator flag.

### Implementation Notes

- Added `role_container_lifecycle_actions` runtime table.
- Added status snapshot and dashboard visibility for container lifecycle
  actions.
- Added `ContainerLifecycleExecutor`.
- `record_plan()` stores planned lifecycle actions without executing commands.
- `execute()` records the planned action, runs the command through the runner,
  and updates the action to `succeeded` or `failed`.
- Successful `start` actions return the role instance from `hydrating` to
  `idle`.
- Added `run-project-container-lifecycle` CLI command:
  - without `--execute`, it records planned actions only
  - with `--execute`, it runs planned commands
  - `--timeout-seconds` controls command timeout

### Tests Added

- executor records successful container lifecycle command output
- successful start/hydration returns the role instance to `idle`
- executor records failed command output and preserves hibernated state
- dashboard/status exposes lifecycle action records
- CLI records planned container lifecycle actions without executing them

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
36 passed
75 passed
```

### QA Rework

- Changed lifecycle action records so each attempt gets a unique `action_id`.
- Added `action_fingerprint` so repeated attempts against the same role,
  service, command, and reason can be correlated without overwriting evidence.
- Added explicit completion updates for the current attempt only.
- Added regression coverage proving repeated failed attempts and later planned
  actions preserve previous failure evidence.

### Known Limitations

- Real Docker execution exists only through the CLI `--execute` flag and is not
  automatically triggered by hibernation maintenance.
- Failure handling records command output but does not yet perform retry,
  rollback, or alert routing.
- Stop action success does not yet change the role-instance status beyond the
  already recorded `hibernated` state.

## PB-005 Story 5 - Bounded Project Supervisor Tick

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add one bounded project supervisor command that runs hibernation maintenance and
then records or executes the resulting container lifecycle actions. This joins
the PB-005 logical hibernation state and container lifecycle action surfaces in
one operator/supervisor entry point without introducing a daemon.

### Implementation Notes

- Added `run-project-supervisor-tick`.
- The command runs project hibernation maintenance first.
- It then runs project container lifecycle handling against the updated durable
  role-instance state.
- By default, container lifecycle actions are recorded as planned only.
- With `--execute`, lifecycle commands are executed through the existing guarded
  executor path.
- JSON output includes both nested hibernation and container lifecycle receipts.

### Tests Added

- supervisor tick hibernates a surplus idle role instance and records a planned
  stop action
- supervisor tick hydrates a hibernated role instance when queued role work
  appears and records a planned start action
- planned lifecycle records remain durable after the two supervisor ticks

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
45 passed
76 passed
```

### Known Limitations

- This is still a bounded command, not an always-on daemon or scheduler.
- `--execute` uses the existing command executor but is not automatically run
  by a background supervisor.
- Retry, rollback, and alert routing for failed lifecycle actions remain later
  slices.

## PB-005 Story 6 - Runtime Attention for Lifecycle Failures

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Make failed role-container lifecycle actions visible as actionable runtime
attention items. A failed Docker Compose start/stop attempt should no longer be
visible only as a failed command record; the operator/status surface should
also show who owns the recovery, why it needs attention, and what the next
retry action is.

### Implementation Notes

- Added a `runtime_attention_items` table for non-connector runtime failures.
- Added `create_runtime_attention_item` and `list_runtime_attention_items` to
  the v2 database repository.
- Container lifecycle executor now records a retryable
  `container_lifecycle_failed` runtime attention item for failed start/stop
  commands.
- Runtime attention is included in `status_snapshot()` counts and data.
- The v2 status dashboard now shows runtime attention alongside connector
  attention and lifecycle action evidence.

### Tests Added

- successful container lifecycle start does not create runtime attention
- failed container lifecycle stop creates a retryable platform-engineer-owned
  runtime attention item linked to the failed action
- repeated failed attempts preserve separate action evidence and separate
  attention source references
- rendered status HTML includes runtime attention details

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
15 passed
76 passed
```

### Known Limitations

- Runtime attention is visible and retry-ready, but no automatic retry policy or
  alert-routing loop is implemented in this story.
- Runtime attention is currently emitted by the container lifecycle executor;
  future supervisor stories should add ownership/closure handling across other
  runtime failure classes.

## PB-005 Story 7 - Lifecycle Attention Resolution on Success

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Close stale runtime attention after a successful retry of the same
role-container lifecycle action target. A previous failure should remain as
evidence, but it should no longer appear as open operator work once a matching
lifecycle action succeeds.

### Implementation Notes

- Added `close_runtime_attention_for_container_lifecycle`.
- Runtime attention closure matches open `container_lifecycle_failed` items by
  the failed action's `action_fingerprint`.
- A successful lifecycle execution now closes matching open failure attention
  and records a `runtime.attention_closed` event.
- The closed attention keeps its original source action reference, preserving
  the failed-attempt evidence while updating the next action to identify the
  successful resolving action.

### Tests Added

- fail-then-success retry on the same lifecycle action closes the prior runtime
  attention item
- closed attention preserves the original failed action reference and names the
  successful resolving action
- unrelated failed lifecycle attention with a different fingerprint remains
  open
- `runtime.attention_closed` is present in the recent event stream

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
16 passed
77 passed
```

### Known Limitations

- This resolves attention only when an identical lifecycle action fingerprint
  later succeeds.
- This story does not add scheduled retries, alert delivery, or operator
  action buttons.

## PB-005 Story 8 - Operator Retry Command for Lifecycle Failures

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a v2 operator command that can retry a failed role-container lifecycle
action from durable action evidence. The command should support safe
plan-only use by default and execute the retry only when explicitly requested.

### Implementation Notes

- Added `get_role_container_lifecycle_action`.
- Added lifecycle action reconstruction from a stored lifecycle action record.
- Added executor methods for retry planning and retry execution.
- Added CLI command `retry-container-lifecycle-action --action-id`.
- The command records a planned retry by default.
- The command executes Docker only when `--execute` is supplied.
- Retry planning/execution reuses the failed action's stored command, working
  directory, service, role, instance, and reason so fingerprints remain
  consistent with runtime-attention closure.

### Tests Added

- executor retry reuses failed action details and action fingerprint
- retry planning rejects unknown and non-failed lifecycle action ids
- successful retry closes previous runtime attention
- CLI plan-only retry records a planned retry without executing Docker
- CLI retry output includes the source failed action id and retry action details

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
40 passed
41 passed after negative retry coverage
79 passed
80 passed after negative retry coverage
```

### Known Limitations

- This story provides an operator command, not automatic retry scheduling.
- The execute path still relies on the configured runtime host having Docker
  Compose access.
- UI action buttons and alert routing remain future slices.

## PB-005 Story 9 - Dashboard Retry Guidance for Runtime Attention

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Surface the lifecycle retry command directly on the v2 status dashboard for
open retryable container lifecycle failures. Operators should not need to infer
or reconstruct the command from raw attention and action records.

### Implementation Notes

- Added an Operator Action column to the Runtime Attention table.
- Open retryable `container_lifecycle_failed` attention now shows:
  - a plan-only retry command
  - an explicit `--execute` retry command
- Rendered commands quote the database path and action id for safer copy/paste.
- Closed or non-retryable runtime attention does not show a retry command.
- The server remains read-only; the dashboard provides commands rather than
  executing lifecycle actions from HTTP.

### Tests Added

- runtime attention HTML includes retry commands for open container lifecycle
  failures
- retry guidance includes the failed lifecycle action id
- closed matching lifecycle attention no longer shows a retry command
- unrelated still-open lifecycle attention continues to show its retry command

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
21 passed
80 passed
```

### Known Limitations

- This story provides copyable command guidance, not executable dashboard
  buttons.
- Commands include the dashboard process database path and assume the operator
  is running from an environment where `agentic_mesh_v2.cli` is available.

## PB-005 Story 10 - Runtime Attention Open/Closed Counts

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Split runtime attention totals into open and closed counts so resolved
container lifecycle failures remain available as evidence without continuing to
look like active operational work.

### Implementation Notes

- Added `runtime_attention_statuses` to the status snapshot.
- Added `runtime_attention_open` and `runtime_attention_closed` count fields.
- Added dashboard tiles for open and closed runtime attention.
- Kept the total runtime attention count unchanged as the evidence total.

### Tests Added

- failed lifecycle action reports one open runtime attention item and zero
  closed items
- successful retry reports one closed runtime attention item and leaves an
  unrelated failed action as one open runtime attention item
- dashboard HTML includes the runtime attention open tile

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_status_dashboard.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
21 passed
80 passed
```

### Known Limitations

- This story adds count visibility only; it does not add filtering controls to
  the dashboard tables.

## PB-005 Story 11 - Planned Lifecycle Action Idempotency

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Prevent repeated plan-only lifecycle operations from creating duplicate planned
container lifecycle action records for the same role instance, command, and
reason. This prepares the supervisor for bounded loops without filling the
audit trail with identical pending actions.

### Implementation Notes

- Added lookup for an existing planned role-container lifecycle action by
  action fingerprint.
- Added `record_plan_if_absent` to reuse an existing planned action rather than
  append a duplicate.
- Plan-only `run-project-container-lifecycle` now reports
  `existing_planned_count` and marks reused actions as `already_planned`.
- Plan-only `retry-container-lifecycle-action` now reuses existing planned
  retry actions and reports `already_planned`.
- Failed and succeeded lifecycle execution records remain append-only.

### Tests Added

- repeated `run-project-container-lifecycle` plan-only calls reuse the existing
  planned action id and leave only one planned action record
- repeated retry planning reuses the existing planned retry action id
- existing planned actions are counted separately from newly planned actions

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
41 passed
80 passed
```

### Known Limitations

- This story deduplicates only open planned actions with the same lifecycle
  action fingerprint. Failed and succeeded execution attempts remain separate
  evidence.

## PB-005 Story 12 - Bounded Project Supervisor Loop

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a bounded repeated project supervisor command that runs the existing
supervisor tick for a fixed number of cycles. This gives operators and future
container entrypoints a safe loop primitive without introducing an always-on
daemon yet.

### Implementation Notes

- Added `run-project-supervisor-loop`.
- Added `--cycles` and `--poll-seconds` bounds matching the project
  role-services loop behavior.
- Each cycle runs hibernation maintenance followed by container lifecycle
  handling through the existing supervisor tick path.
- JSON output includes per-cycle receipts plus aggregate hibernation and
  container lifecycle totals.
- Plan-only lifecycle idempotency means repeated cycles reuse an existing
  planned lifecycle action instead of appending duplicates.

### Tests Added

- bounded supervisor loop runs two cycles and aggregates hibernation and
  container lifecycle totals
- second plan-only cycle reports `already_planned` for the existing lifecycle
  action
- repeated supervisor cycles leave only one planned lifecycle action record
- invalid cycle and poll bounds are rejected

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_container_lifecycle.py tests\test_v2_hibernation.py tests\test_v2_project_config.py tests\test_v2_role_assignment_execution.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
26 passed
83 passed
```

### Known Limitations

- This is still bounded/operator-controlled execution, not an always-on daemon
  or host-level service supervisor.
- The loop delegates lifecycle execution behavior to the existing `--execute`
  flag and does not add new retry scheduling or alert delivery.

## PB-005 Story 13 - Dashboard Supervisor Command Guidance

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Expose copyable bounded supervisor commands on the v2 status dashboard when
the status server is started with a project file. This gives operators a
visible path to run the existing project supervisor tick or loop without
guessing the required DB/project CLI arguments.

### Implementation Notes

- Added optional `serve --project-file`.
- The status dashboard banner now shows the configured project file when one
  is provided.
- Added a read-only `Supervisor Commands` dashboard section.
- When the project file is configured, the dashboard renders copyable commands
  for:
  - plan-only supervisor tick
  - execute supervisor tick
  - plan-only bounded supervisor loop
  - execute bounded supervisor loop
- When no project file is configured, the dashboard explicitly says to restart
  the status server with `--project-file` and does not fake project-scoped
  commands.

### Tests Added

- status HTML shows project-file-rooted supervisor commands when configured
- status HTML does not render supervisor commands without a configured project
  file

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_status_dashboard.py tests\test_v2_cli_server.py
```

Results:

```text
31 passed
```

### Known Limitations

- This story exposes command guidance only. It does not add HTTP-side action
  buttons, a background daemon, or automatic supervisor scheduling.

## PB-005 Story 14 - Project Supervisor Service Entrypoint

Status: implemented, QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add an explicit project supervisor service command suitable for container
entrypoints and operator-managed long-running execution. The command reuses
the existing project supervisor tick so hibernation maintenance and container
lifecycle planning/execution stay on the same path as bounded operator runs.

### Implementation Notes

- Added `run-project-supervisor-service`.
- Service mode must be explicit:
  - `--cycles N` runs a bounded service session and exits.
  - `--continuous` runs until interrupted by the host/container supervisor.
- Preserved existing `--execute`, `--poll-seconds`, `--timeout-seconds`,
  `--hibernate-reason`, and `--hydrate-reason` behavior.
- JSON receipts include `service_mode`, `cycles_requested`,
  `cycles_completed`, aggregate hibernation totals, aggregate container
  lifecycle totals, and per-cycle receipts.
- `KeyboardInterrupt` is handled as a clean `interrupted` service receipt.
- `cycles_completed` counts completed cycles only, not an interrupted in-flight
  attempt.
- The dashboard supervisor command guidance now includes a continuous
  supervisor service command.

### Tests Added

- bounded supervisor service runs two cycles and aggregates hibernation and
  lifecycle totals
- repeated service cycles reuse planned lifecycle actions via existing
  idempotency
- invalid service cycle and poll bounds are rejected
- service mode is required so continuous execution is deliberate
- continuous service interruption reports `interrupted` and completed-cycle
  count accurately
- status dashboard includes the continuous supervisor service command only when
  a project file is configured

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
```

Results:

```text
36 passed
```

### Known Limitations

- This story adds the service entrypoint, not the Docker Compose/systemd/K8s
  deployment wiring that will keep the command running in a deployed runtime.
- Continuous mode remains host-supervisor controlled and writes its summary
  only when interrupted.

## PB-005 Story 15 - Dogfood Compose Supervisor Service Wiring

Status: QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Wire the new project supervisor service entrypoint into the dogfood Docker
Compose deployment so the installed v2 runtime has a long-running supervisor
container, not only an operator CLI command.

### Implementation Notes

- Updated the dogfood `v2-runtime` service to start the status server with
  `--project-file /mesh/project/agentic-mesh/project.yaml`.
- Added a `v2-supervisor` Compose service using the shared Agentic Mesh image,
  environment, volumes, and working directory.
- The supervisor service runs:
  `run-project-supervisor-service --continuous --execute`.
- Added `AGENTIC_MESH_SUPERVISOR_POLL_SECONDS` interpolation with a default of
  five seconds for local tuning.
- Added `depends_on: v2-runtime` so the status/runtime service can initialize
  the DB before the supervisor starts, while the supervisor still migrates the
  DB defensively through the CLI path.
- Updated the linuxch overlay so `v2-supervisor` restarts unless stopped.

### Tests Added

- dogfood Compose starts the status server with a project file
- dogfood Compose includes a `v2-supervisor` service running the continuous
  supervisor command with project file, poll interval, and execute flag
- linuxch overlay includes restart policy for the supervisor service

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_dogfood_compose.py tests\test_v2_cli_server.py tests\test_v2_status_dashboard.py
docker compose -f examples\projects\agentic-mesh-dev\deploy\compose\docker-compose.yml -f examples\projects\agentic-mesh-dev\deploy\compose\docker-compose.linuxch.yml config --quiet
```

Results:

```text
39 passed
docker compose config passed
```

### Known Limitations

- This story wires the dogfood Docker Compose deployment only. Systemd,
  Kubernetes, Helm, Terraform, and generated deployment templates remain future
  slices.
- The supervisor service loops the hibernation/container lifecycle supervisor;
  long-running role worker service containers are still represented by later
  deployment/profile stories.

## PB-005 Story 16 - Single Role Service Entrypoint

Status: QA accepted.

Owner role: Engineering

Date: 2026-06-12

### Scope

Add a long-running service entrypoint for one role-agent instance so deployed
role containers can run their own claim/recover/process loop instead of relying
on a project-wide operator loop.

### Implementation Notes

- Added `run-role-service-loop`.
- The command targets one explicit `--role-id` and `--role-instance-id`.
- Worker config is loaded through the existing role-service worker path:
  `--project-file` for project YAML worker config, or explicit worker override
  arguments for tests/operators.
- Service mode must be explicit:
  - `--cycles N` runs a bounded role-service session and exits.
  - `--continuous` runs until interrupted by the host/container supervisor.
- The existing `run-role-service-tick` path now shares the same helper as the
  loop, so one-shot and service execution record runs and safe outputs
  identically.
- JSON receipts include service mode, role id, role instance id, project file,
  completed cycle count, recovered/processed totals, and per-cycle receipts.
- `KeyboardInterrupt` is handled as a clean `interrupted` service receipt.

### Tests Added

- single role-service loop processes queued role work across bounded cycles
- invalid role-service loop cycle and poll bounds are rejected
- role-service loop requires explicit bounded or continuous mode
- continuous role-service interruption reports completed cycles and totals

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_cli_server.py
```

Results:

```text
36 passed
```

### Known Limitations

- This story adds the single role-service entrypoint only. Dogfood Compose
  role-agent containers remain the next deployment/profile slice.
- The command still uses the currently supported worker adapters; Codex-worker
  execution remains behind the worker-adapter implementation boundary.

## PB-005 Story 17 - Command-Backed Codex CLI Worker Adapter

Status: implemented, QA accepted after rework.

Owner role: Engineering

Date: 2026-06-12

### Scope

Make `adapter: codex-cli` a real worker adapter instead of an unsupported
future placeholder. This unblocks project role-service construction for roles
configured with Codex CLI workers while keeping durable effects behind
safe-output JSON.

### Implementation Notes

- Added `CodexCliWorker`.
- The adapter runs a configured command, sends assignment and worker metadata
  as JSON on stdin, and parses safe-output JSON from stdout.
- Worker metadata includes adapter name, model, reasoning effort, sandbox mode,
  and credential reference metadata without exposing secret material.
- Added default command construction for `codex-cli` from `executable` and
  `args`, defaulting to `codex exec`.
- Added a longer default timeout of four hours for Codex worker runs.
- Added validation for command shape, timeout type, reasoning effort,
  sandbox mode, and auth mapping.
- Added CLI `--worker codex-cli` support for one-shot and role-service loop
  commands.
- Updated project schema worker properties to allow command-backed worker
  fields: `command`, `executable`, and `args`.
- Unknown future adapters remain explicit and skippable via existing
  `--skip-unsupported` project-runner behavior.

### Tests Added

- adapter factory creates and runs a configured `codex-cli` worker
- adapter factory creates a default `codex-cli` worker command
- invalid `codex-cli` worker config is rejected
- project-file role-service execution runs a configured `codex-cli` worker
- project runner still skips genuinely unsupported future adapters
- project schema accepts command-backed `codex-cli` worker configuration

### Tests Run

```text
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider tests\test_v2_worker_adapters.py tests\test_v2_cli_server.py tests\test_v2_project_config.py tests\test_v2_project_schema.py
python -m agentic_mesh.cli validate-config
```

Results:

```text
61 passed before QA rework
64 passed after QA rework
validate-config not available in this v2 branch environment: No module named agentic_mesh
```

Additional schema check:

```text
Direct validation of the full dogfood project.yaml against project.schema.json
still fails on pre-existing schema drift: release_deployment_targets is present
in the project file but absent from the schema.
```

### Known Limitations

- This story does not assemble the final XML-section role prompt or map Codex
  CLI-specific flags beyond command metadata. Prompt assembly and exact Codex
  invocation policy remain a later prompt-system slice.
- The adapter requires Codex-compatible commands to emit safe-output JSON on
  stdout. Non-JSON model text remains a worker failure until safe-output tool
  transport is implemented.

### QA Rework

QA found that the direct CLI `--worker codex-cli` path injected the generic
300-second timeout and overrode the adapter's four-hour Codex default.

Rework:

- Changed direct CLI worker timeout parsing so `--worker-timeout-seconds` is
  optional.
- Direct CLI worker config now only includes `timeout_seconds` when the operator
  explicitly supplies a timeout.
- Added regression tests proving `--worker codex-cli` preserves the adapter
  default and still accepts an explicit timeout override.
- Preserved adapter defaults for `safe-output-subprocess` in the same way.

## Review Log

- RL-001 | engineering | implementation | PB-004 Story 1 | Added
  claim-and-run role assignment execution with terminal assignment state and
  dashboard visibility. | QA accepted 2026-06-12
- RL-002 | engineering | implementation | PB-004 Story 2 | Added
  safe-output-driven handoff/consult assignment materialization with inherited
  work item context and no Teams delivery side effects. | QA accepted
  2026-06-12
- RL-003 | engineering | implementation | PB-004 Story 3 | Added terminal
  assignment outcome states for human waits, blockers, incomplete runs, and
  explicit no-op outcomes. | QA accepted 2026-06-12
- RL-004 | engineering | implementation | PB-004 Story 4 | Added bounded
  role-service draining, role-instance heartbeat/status records, and dashboard
  visibility. | QA accepted 2026-06-12
- RL-005 | engineering | implementation | PB-004 Story 5 | Added role
  assignment claim leases, explicit lease refresh, stale-claim recovery, and
  dashboard lease/recovery visibility. | QA accepted 2026-06-12
- RL-006 | engineering | implementation | PB-004 Story 6 | Added a
  role-scoped service maintenance tick that recovers stale assignments before
  draining queued work and records recovered/processed status. | QA accepted
  2026-06-12
- RL-007 | engineering | implementation | PB-004 Story 7 | Added an operator
  CLI command for bounded role-scoped stale assignment recovery with auditable
  JSON output. | QA accepted 2026-06-12
- RL-008 | engineering | implementation | PB-004 Story 8 | Added a
  deterministic safe-output-file worker adapter and CLI role-service tick runner
  with bounded recovery/drain options and JSON run receipts. | QA accepted
  2026-06-12
- RL-009 | engineering | implementation | PB-004 Story 9 | Added a subprocess
  safe-output worker adapter and CLI wiring for external command execution with
  assignment stdin, safe-output stdout parsing, and failure capture. | QA
  accepted 2026-06-12
- RL-010 | engineering | implementation | PB-004 Story 10 | Added a shared
  worker-adapter config factory and moved CLI role-service tick construction
  through it. | QA accepted 2026-06-12
- RL-011 | engineering | implementation | PB-004 Story 11 | Added project YAML
  role worker config loading and wired `run-role-service-tick --project-file`
  into the shared worker factory path. | QA accepted 2026-06-12
- RL-012 | engineering | implementation | PB-004 Story 12 | Added project
  role-service instance expansion and a CLI command to run one bounded tick for
  each configured role instance. | QA accepted 2026-06-12
- RL-013 | engineering | implementation | PB-004 Story 13 | Added a bounded
  project role-service loop command with cycle/poll validation and per-cycle
  JSON receipts. | QA accepted 2026-06-12
- RL-014 | engineering | implementation | PB-005 Story 1 | Added hibernation
  policy loading, safe-point evaluation, durable hibernation/hydration state,
  and dashboard evidence. | QA accepted 2026-06-12
- RL-015 | engineering | implementation | PB-005 Story 2 | Added project
  hibernation maintenance with warm-pool hibernation, queued-work hydration,
  and JSON per-instance receipts. | QA accepted 2026-06-12
- RL-016 | engineering | implementation | PB-005 Story 3 | Added Docker Compose
  container lifecycle command planning for hibernated and hydrating role
  instances. | QA accepted after rework 2026-06-12
- RL-017 | engineering | QA rework | PB-005 Story 3 | Rejected non-string
  Compose file config and non-exact template field expressions after QA found
  validation gaps. | QA accepted 2026-06-12
- RL-018 | engineering | implementation | PB-005 Story 4 | Added durable
  container lifecycle action records, guarded execution, status visibility, and
  planned-action CLI recording. | QA accepted after rework 2026-06-12
- RL-019 | engineering | QA rework | PB-005 Story 4 | Made lifecycle action
  attempts append-only with correlation fingerprints and preserved repeated
  failure evidence after QA found overwrite risk. | QA accepted 2026-06-12
- RL-020 | engineering | implementation | PB-005 Story 5 | Added a bounded
  project supervisor tick that chains hibernation maintenance and container
  lifecycle action handling. | QA accepted 2026-06-12
- RL-021 | engineering | implementation | PB-005 Story 6 | Added runtime
  attention records for failed role-container lifecycle actions, linked them
  to failed action evidence, and surfaced them on the v2 dashboard. | QA
  accepted 2026-06-12
- RL-022 | engineering | implementation | PB-005 Story 7 | Closed matching
  runtime lifecycle-failure attention after successful retry while preserving
  failed action evidence and recording a closure event. | QA accepted
  2026-06-12
- RL-023 | engineering | implementation | PB-005 Story 8 | Added an operator
  CLI command to plan or execute a retry from failed role-container lifecycle
  action evidence while preserving fingerprint-based attention closure. |
  QA accepted 2026-06-12
- RL-024 | engineering | implementation | PB-005 Story 9 | Added dashboard
  retry guidance for open retryable lifecycle failure attention without adding
  HTTP-side lifecycle execution. | QA accepted 2026-06-12
- RL-025 | engineering | implementation | PB-005 Story 10 | Added
  open/closed runtime attention counts so resolved lifecycle failures remain
  evidence without inflating active operator work. | QA accepted 2026-06-12
- RL-026 | engineering | implementation | PB-005 Story 11 | Reused existing
  planned lifecycle action records for duplicate plan-only lifecycle and retry
  commands while preserving append-only execution evidence. | QA accepted
  2026-06-12
- RL-027 | engineering | implementation | PB-005 Story 12 | Added a bounded
  project supervisor loop with cycle/poll validation, aggregate JSON receipts,
  and idempotent plan-only lifecycle handling. | QA accepted 2026-06-12
- RL-028 | engineering | implementation | PB-005 Story 13 | Added v2 status
  dashboard guidance for copyable project supervisor tick/loop commands when a
  project file is configured. | QA accepted 2026-06-12
- RL-029 | engineering | implementation | PB-005 Story 14 | Added an explicit
  project supervisor service command with bounded and continuous modes,
  interrupt receipts, and dashboard command guidance. | QA accepted
  2026-06-12
- RL-030 | engineering | implementation | PB-005 Story 15 | Wired the dogfood
  Compose deployment to run the v2 supervisor service continuously and start
  the status server with project-file context. | QA accepted 2026-06-12
- RL-031 | engineering | implementation | PB-005 Story 16 | Added a
  single-role service loop entrypoint for one role-agent instance with bounded
  and continuous modes, shared tick execution, and interrupt receipts. |
  QA accepted 2026-06-12
- RL-032 | engineering | implementation | PB-005 Story 17 | Added a
  command-backed Codex CLI worker adapter, CLI selection support, and schema
  support for command-backed worker config. | QA requested rework 2026-06-12
- RL-033 | engineering | QA rework | PB-005 Story 17 | Preserved adapter-level
  timeout defaults for direct CLI worker overrides so `codex-cli` keeps its
  four-hour default unless an operator explicitly supplies a timeout. |
  QA accepted 2026-06-12
