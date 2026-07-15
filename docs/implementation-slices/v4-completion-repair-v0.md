# V4 Bounded Completion Repair

## Problem

A V4 role can finish a Codex turn without recording the durable safe-output
required by its completion contract. The runtime currently closes that message
and immediately queues Project Manager attention. A routine omitted handoff can
therefore strand an otherwise healthy work item in management recovery.

## Scope

Add one bounded same-role corrective turn before Project Manager escalation.
The repair must continue the existing role thread, identify the exact missing
completion predicates, avoid repeating completed work, and retain the original
correlation id. A second incomplete turn must escalate rather than loop.

## Acceptance Criteria

- A first `completed_with_missing_output` result queues exactly one repair
  message to the same role.
- The repair message carries an explicit completion contract containing the
  missing predicates and identifies the original message and diagnostic.
- The role keeps its existing Codex thread so the repair has the prior turn's
  context.
- A failed repair queues no further repair and escalates once to Project
  Manager with `completion_repair_exhausted` evidence.
- Deterministic repair message identity prevents duplicate queue entries if the
  completion path is replayed.
- Human Teams/API/CLI messages retain their existing higher queue priority.
- V4 runtime regression tests pass against PostgreSQL.

## Out Of Scope

- Automatically fabricating a specialist decision or safe-output call.
- Retrying command, permission, or provider failures already covered by other
  recovery policies.
- Advancing a successor work item without the required role-owned output.
