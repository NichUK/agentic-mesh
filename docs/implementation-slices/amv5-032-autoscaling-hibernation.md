# AMV5-032 — Autoscaling and hibernation

## Outcome

The control plane reconciles each configured role against durable queue and
instance state. A bounded, pre-registered instance pool scales out when ready
work has waited 60 seconds and scales to zero after five idle minutes when the
role policy permits it.

## Decisions

- Project configuration pre-registers stable role-instance identities. The
  scaler wakes and hibernates those identities; it does not create personas.
- Policies default to a 60-second scale-out threshold and 300-second idle
  grace period. Minimum warm and maximum running counts remain configurable.
- A role at zero running instances wakes one immediately for ready work. A
  busy role adds capacity after the oldest ready item reaches its threshold.
- Project Manager policies require at least one warm instance and cannot
  enable idle hibernation.
- A persisted `starting` or `hibernating` transition and action id are the
  desired state. The injected supervisor must apply an action idempotently;
  interrupted reconciliation safely repeats the same action.
- Hibernation fails closed while an instance has any unreleased work lease, an
  active thread operation, or pending instance-correlated outbox data.
- Thread bindings, role identity and queued work remain durable outside the
  worker process. Hibernation never deletes them.
- The control API starts an automatic reconciliation loop only when a concrete
  supervisor adapter is injected by the deployment profile. Product semantics
  remain independent of Docker, Compose or another orchestrator.

## Acceptance criteria

1. Ready work at a zero-sized role wakes one stable configured instance
   immediately.
2. When all running capacity is busy, work waiting at least 60 seconds wakes
   one additional instance without exceeding the configured maximum.
3. Repeated or concurrent reconciliation reuses the persisted action id and
   cannot duplicate the external start or stop operation.
4. Surplus specialist instances hibernate after five idle minutes, down to the
   configured minimum, and may scale to zero.
5. Unreleased work leases, active thread operations and pending durable output
   prevent hibernation even after the idle threshold.
6. Hibernation preserves queue items, work ownership, prompt-pinned thread
   affinity and role-instance identity.
7. Project Manager configuration always retains one warm instance.
8. Policy, instance lifecycle evidence and manual reconciliation are available
   through authenticated project/organization control APIs; foreign-project
   access fails closed.
9. With a supervisor adapter installed, the API lifespan continuously
   reconciles the fleet at a bounded configurable interval.

## Test plan

- Exercise scale-to-zero wake, a busy burst older than 60 seconds, maximum
  limits and minimum-warm preservation.
- Simulate reconciliation interruption and concurrent ticks with an idempotent
  fake supervisor.
- Attempt hibernation with leases, active thread operations and pending outbox
  records, then verify hibernation only after each safe-point blocker clears.
- Verify thread-affinity and queue records survive hibernation and wake.
- Exercise policy validation, Project Manager guardrails, API authorization,
  automatic lifespan reconciliation and migration upgrade paths.
- Run focused, full V5, known V4 baseline, package/secret and clean-clone checks.
