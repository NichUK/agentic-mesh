# AMV5-003 Retired Project Removal

## Outcome

Remove Quantauma from active Agentic Mesh source, runtime terminology, test
fixtures, project registrations, deployment materialization, connector
bindings, and credential references while retaining historical documentation.

## Implementation

- replace named role-instance and multi-project fixtures with
  `example-project`;
- generalize the dormant V4 migration guard to `retired_project_messages`;
- verify active source, tests, configuration, examples, scripts, and workflow
  files contain no named-project reference;
- allow historical architecture, memory, decisions, and Git history to remain.

The repository contains no active project registration, queue, connector,
deployment, or credential binding for the retired project. Neutral fixtures
continue to exercise project isolation and migration behaviour without keeping
the former project operationally addressable.

## Acceptance Evidence

Run:

```powershell
pytest -q tests/test_v5_retired_project_removal.py
pytest -q tests/test_v4_shared_fleet_binding.py tests/test_v4_shared_fleet_dashboard.py
```
