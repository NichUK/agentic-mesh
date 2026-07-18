# AMV5-041 — External flow engine

## Outcome

Execute a validated, digest-pinned external flow over durable V5 work. The engine decides only flow
state and required actions; existing lifecycle, queue, handoff, approval, document, and event services
retain their own authority.

## Boundary

- Load the `flow` object from a resolved external configuration and pin its digest plus normalized
  snapshot when a run starts. Package activation never changes an in-flight run.
- Reject unknown entry/terminal/route states, missing owners, owner/target-role disagreement, duplicate
  ids, unsupported conditions, ambiguous matching routes, non-terminal dead ends, and unreachable states.
- Store one optimistic current run and immutable transition journal in Postgres. Stable operation ids
  make start, prepare, and pickup retries deterministic.
- Entering a state materializes its owner, artifact path, applicable consults, gates, and informs as
  durable obligations. Conditions use exact field membership only; absent fields do not match.
- `prepare_transition` requires applicable gates and the state artifact to be externally verified, then
  selects exactly one route and offers ownership through the existing accepted-handoff service.
- The current state/owner do not change when a handoff is offered or merely claimed. `pickup_transition`
  moves them only after the exact target handoff is accepted.
- Terminal states have no outbound routes. Completing them records terminal completion through the
  existing lifecycle service; terminal error remains governed by the existing reliability policy.
- Consults and informs are non-owning routed actions. They cannot advance state or satisfy a gate merely
  by delivery; later governance stories add RACI and specialized gate policy.
- Do not parse prompts, infer routes with a model, duplicate artifacts in Postgres, or add a general BPMN
  engine. External package content supplies behavior; adapters supply side effects and evidence.

## Acceptance criteria

- The external SDLC package validates and a golden run follows its configured owners and routes.
- `architecture_impact` selects the no-material or enterprise path exactly as configured.
- Invalid, ambiguous, unreachable, owner-mismatched, and dead-end flows cannot start.
- Active runs retain their original flow digest/snapshot after another package version activates.
- Every state exposes one accountable owner and its applicable artifact, consult, gate, and inform work.
- State ownership changes only after the configured target accepts the durable handoff.
- Restart and exact operation replay cannot lose, duplicate, or skip a transition.
