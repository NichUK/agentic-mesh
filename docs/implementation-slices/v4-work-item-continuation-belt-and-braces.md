# V4 Work-Item Continuation: Belt-and-Braces Stories

## Purpose

Prevent a nonterminal work item from silently stopping, regardless of whether
the immediate cause is an agent turn, handoff, queue, connector, external
system, deployment, or human decision wait.

This slice treats continuation as a runtime invariant, not as prompt advice.
Prompts still tell roles what good ownership looks like, but durable state,
transactions, reconciliation, watchdogs, alerts, and recovery provide
independent layers of protection.

## Program Invariant

Every nonterminal work item must have at least one current, executable
continuation recorded durably:

- an active role turn with a fresh lease;
- a queued role message or acknowledged handoff;
- a delivered human decision request with a correlation identifier and a
  response deadline; or
- a scheduled external-state reconciliation with a due time.

An owner name, a prose `next_action`, an open pull request, or a status such as
`awaiting_review` is not by itself an executable continuation.

If the runtime cannot prove a valid continuation, it must create a
deterministic recovery action or escalate visibly. It must never leave the
work item merely idle and hope that a person notices.

## Guardrails

- Recovery operations are idempotent and safe to repeat.
- One logical transition creates at most one successor action.
- Project identity, work-item identity, role instance, correlation identifier,
  and causation identifier travel through every continuation.
- Automatic correction and review are bounded to three loops. Failure after
  the third loop becomes a concise sponsor decision, not an unbounded retry.
- A work item is complete only when its acceptance evidence is recorded and no
  required successor, notification, review, or external action remains.
- Diagnostic states such as `completed_with_missing_output` are never accepted
  as successful work-item completion.

## Story 1: Make Continuation a First-Class Durable Record

**As a** runtime operator, **I want** every nonterminal work item to reference a
machine-executable continuation, **so that** an apparently owned item cannot
silently become inert.

### Acceptance criteria

1. The data model represents active-turn, queued-message, human-decision, and
   external-reconciliation continuations explicitly.
2. Each continuation records project, work item, owner role instance, status,
   creation time, due time, attempt count, correlation, and causation.
3. Nonterminal transitions without a valid continuation fail closed unless
   they atomically create a deterministic recovery continuation.
4. Status and agent pages show the continuation type, current owner, age, due
   time, and last progress time.
5. Prose fields such as `next_action` cannot satisfy the invariant.

## Story 2: Commit State and Dispatch Atomically

**As a** work-item owner, **I want** state changes and their successor dispatch
to be committed together, **so that** a crash cannot save the new state while
losing the handoff.

### Acceptance criteria

1. Work-item transitions and outbound continuations use one database
   transaction and a durable outbox.
2. The dispatcher marks an outbox record delivered only after the target queue
   has durably accepted it.
3. A crash before acceptance causes safe redelivery after restart.
4. Stable idempotency keys prevent duplicate role turns, cards, and handoffs.
5. Tests cover failure before commit, after commit, during delivery, and after
   delivery but before acknowledgement.

## Story 3: Make Handoffs Acknowledged Ownership Transfers

**As a** sending role, **I want** ownership to transfer only when the receiver's
work is durably queued, **so that** neither role can reasonably assume the other
one owns a missing handoff.

### Acceptance criteria

1. A handoff has `proposed`, `queued`, `accepted`, `completed`, and `failed`
   states with timestamps.
2. The sender remains accountable until the receiver's queue acknowledges the
   handoff.
3. Rejected, unroutable, or expired handoffs return to the sender or Project
   Manager with the actionable failure reason.
4. Duplicate delivery reuses the same logical handoff and does not start a
   second implementation.
5. The UI distinguishes waiting to hand off from work accepted by the target.

## Story 4: Repair Incomplete Agent Turns Automatically

**As a** role agent, **I want** one bounded opportunity to supply missing
durable outputs, **so that** a good turn is not stranded by an omitted safe
output.

This story incorporates
[`v4-completion-repair-v0.md`](./v4-completion-repair-v0.md) as the minimum
behaviour and extends its evidence into the program invariant.

### Acceptance criteria

1. A first `completed_with_missing_output` result queues exactly one same-role
   repair turn on the same thread and correlation chain.
2. The repair prompt names the missing durable output and the precise action
   required.
3. A second incomplete result escalates exactly once to Project Manager.
4. Repair and escalation identities are deterministic across restarts and
   duplicate completion events.
5. A repaired turn either creates its required successor continuation or
   reaches a proven terminal state.

## Story 5: Recover Orphaned and Stale Active Turns

**As a** service owner, **I want** active turns to hold renewable leases, **so
that** process loss, disconnected remote-control sessions, and container
restarts cannot leave permanent phantom activity.

### Acceptance criteria

1. An active turn has a renewable lease and records useful progress separately
   from process liveness.
2. A turn with an expired lease is reconciled against the worker before being
   declared orphaned.
3. An orphan is resumed or safely retried with the same logical turn identity;
   it is not duplicated.
4. Exhausted recovery attempts escalate with the last confirmed progress,
   failure reason, and smallest useful human decision.
5. Tests cover agent termination, dispatcher termination, network loss,
   websocket loss, and full runtime restart.

## Story 6: Reconcile External Work Until It Is Truly Finished

**As a** project role, **I want** waits on pull requests, checks, deployments,
and other external systems to remain scheduled work, **so that** `awaiting_*`
does not mean nobody will ever look again.

### Acceptance criteria

1. Every external wait records the resource identity, expected condition,
   reconciliation deadline, and next poll time.
2. Webhooks trigger immediate reconciliation where available; persisted polling
   provides a fallback when callbacks are missed.
3. Check completion, PR review, merge, deployment completion, rejection, and
   timeout each advance or escalate the originating work item exactly once.
4. Credential and permission failures are recorded as actionable blockers and
   routed to a role or sponsor able to resolve them.
5. A restored credential automatically resumes the existing continuation; it
   does not require a new feature dispatch.

## Story 7: Guarantee Human Decision Delivery and Resumption

**As a** sponsor, **I want** decision requests delivered through the configured
collaboration channel and tracked to resolution, **so that** an internal
`awaiting_approval` state cannot wait on a message I never received.

### Acceptance criteria

1. Entering a human-wait state atomically creates a durable notification
   continuation.
2. The state becomes `awaiting_human_response` only after Teams or another
   configured connector confirms delivery.
3. Failed delivery retries with bounded backoff and then escalates through a
   configured fallback path visible to operators.
4. The card contains a short plain-language summary of the actual question,
   why it matters, the recommended choice, and the consequences of each option;
   an opaque work-item identifier is supporting detail only.
5. A response resumes the same work item exactly once, closes the wait, and
   records the responder and decision evidence.
6. Overdue requests are reminded and escalated according to policy rather than
   remaining silent.

## Story 8: Enforce Project Scope at Every Boundary

**As a** sponsor of several projects, **I want** one shared agent fleet to
preserve project scope on every action, **so that** an idle or stale context
cannot make an Agentic Mesh role dispatch Quantauma work or vice versa.

### Acceptance criteria

1. Project identity is mandatory on work items, messages, turns, handoffs,
   human cards, external reconciliations, and safe outputs.
2. The runtime rejects a transition whose project does not match its parent
   work item and records the attempted mismatch.
3. An agent wake-up receives its project-scoped context from the durable work
   message, not from whichever project it handled most recently.
4. Cross-project references require an explicit, auditable relationship and
   cannot silently change ownership or scope.
5. Tests interleave work from at least two projects through the same role
   instances and prove isolation without creating separate fleets.

## Story 9: Keep Role Capacity and Queue Waits Visible

**As a** Project Manager, **I want** work waiting for a busy role to remain an
aged, scheduled continuation, **so that** single-fleet contention cannot look
like unexplained inactivity.

### Acceptance criteria

1. Queued work exposes queue position, age, priority, role capacity, and the
   reason it is not running.
2. Fair scheduling prevents one project from indefinitely starving another
   unless an explicit priority policy says otherwise.
3. Queue-age service levels generate alerts and, when safe, increase role
   instance capacity within configured limits.
4. Capacity changes do not create a project-specific duplicate fleet.
5. A role-capacity wait remains a valid continuation only while its queue and
   scheduling deadline are healthy.

## Story 10: Add an Independent Continuation Watchdog

**As an** operator, **I want** a periodic invariant sweep independent of the
normal transition path, **so that** defects in the primary flow are detected
and repaired by a second line of defence.

### Acceptance criteria

1. The watchdog scans for nonterminal work without a valid continuation, stale
   leases, overdue queues, undelivered decisions, expired external waits,
   cross-project inconsistencies, and terminal work with live successors.
2. Safe repairs use deterministic identities and are recorded as audit events.
3. Unsafe or repeatedly failed repairs create one concise Project Manager or
   sponsor escalation with evidence and a deadline.
4. The watchdog runs on startup and on a configurable schedule.
5. Failure of the watchdog itself raises an operational alert.
6. No nonterminal item remains continuation-less for longer than one watchdog
   interval plus dispatch latency.

## Story 11: Instrument Continuation Health and Alert on Risk

**As a** platform operator, **I want** logs, traces, metrics, dashboards, and
alerts for the full continuation lifecycle, **so that** degradation is visible
before a sponsor discovers idle agents.

### Acceptance criteria

1. OpenTelemetry traces link the originating turn, safe output, transition,
   outbox delivery, target turn, external reconciliation, and notification.
2. Structured logs include project, work item, role instance, continuation,
   correlation, causation, attempt, and outcome without exposing secrets.
3. Metrics include nonterminal items without valid continuations, continuation
   age, queue age, stale leases, delivery retries, repair turns, escalations,
   overdue human waits, external-wait age, and tokens in/out per agent and
   project.
4. Alerts exist for invariant breaches, watchdog failure, sustained queue-age
   breach, delivery exhaustion, and external reconciliation exhaustion.
5. Dashboards present one shared fleet with project filters rather than
   contradictory project-specific fleet views.

## Story 12: Prove Recovery with Fault Injection and Release Gates

**As a** release manager, **I want** continuation resilience tested under
realistic failures, **so that** the guarantee is demonstrated rather than
inferred from happy-path unit tests.

### Acceptance criteria

1. Automated tests inject crashes at each side of the state/outbox boundary,
   duplicate callbacks, missed callbacks, queue outages, database failover,
   connector failure, worker disconnect, role saturation, credential loss, and
   runtime redeployment.
2. Tests include two projects interleaved through one shared fleet.
3. Every scenario reaches a proven terminal state or a delivered, actionable
   human wait within a defined service level, without duplicate implementation.
4. Restart and upgrade tests prove all pending continuations survive and are
   reconciled on startup.
5. Release promotion is blocked when invariant, recovery, or notification
   delivery tests fail.
6. A production-like soak test proves that no nonterminal work item is left
   without a continuation throughout the observation period.

## Delivery Order

1. **Foundation:** Stories 1, 2, and 8 establish durable identity, atomicity,
   and project isolation.
2. **Primary continuation:** Stories 3, 4, 5, 6, 7, and 9 close the known
   handoff, turn, external, human, and capacity gaps.
3. **Independent safety net:** Stories 10 and 11 detect and surface defects in
   the primary mechanisms.
4. **Proof and promotion:** Story 12 makes resilience a release gate.

Stories should be delivered as small reviewable slices. Temporary migrations
may introduce the new continuation model in shadow/audit mode, but live
enforcement must not be enabled until existing nonterminal work has been
backfilled or explicitly reconciled.

## Program Definition of Done

This program is complete only when all of the following are true:

- the runtime prevents new continuation-less nonterminal states;
- the independent watchdog detects and repairs any state created outside that
  path;
- role, external-system, and human waits all have durable scheduled follow-up;
- connector and credential failures create visible, actionable ownership;
- restarts and upgrades neither lose nor duplicate work;
- one shared fleet safely interleaves multiple projects;
- telemetry and alerts identify continuation risk without relying on a human
  watching the agents page; and
- fault-injection evidence demonstrates eventual continuation or proven
  completion for every tested failure mode.
