# AMV5-049 — Milestone progress synchronization to ADO

## Outcome

Publish concise, ordered V5 delivery milestones to a linked ADO work item while
preserving V5 authority, suppressing duplicate/noisy events, and never silently
overwriting a human's ADO state change.

## Scope and boundaries

- Add one durable milestone publisher on top of the AMV5-048 project-bound ADO
  adapter. Do not mirror the event journal or create a second lifecycle.
- Accept only `start`, `handoff`, `blocker`, `recovery`, `pull-request`,
  `deployment`, and `acceptance` milestones.
- Require a concise summary, typed evidence references, and one next action.
  Render those three elements in every published comment.
- Persist the milestone and its fingerprint before ADO effects. A stable compact
  marker line lets retry find a comment already created before a crash. ADO
  strips HTML comments, so the identifier must remain visible in stored text.
- Sequence milestones per V5 work item. Exact replay returns its recorded result;
  a lower late sequence is suppressed; a later sequence waits while an earlier
  one remains pending.
- Map `start` from `New` to `Active`. Map pull-request/deployment to `Resolved`
  only with both implementation and automated-test evidence. Map acceptance from
  `Resolved` to `Closed` only with acceptance and owner-review evidence.
- If the current ADO state is neither the requested state nor its expected
  predecessor, publish the milestone comment but preserve that manual state and
  record the disposition.
- Keep transport retries and project validation in the AMV5-048 adapter. ADO
  failure leaves the milestone pending and never changes V5 work state.

## Acceptance criteria

- Every supported milestone produces at most one useful ADO comment containing
  status, summary, evidence, and next action.
- Exact duplicate delivery is idempotent, stale delivery is visibly suppressed,
  and out-of-order delivery cannot overtake an earlier pending milestone.
- Start, Resolved, and Closed mappings follow the approved evidence rules.
- Manual ADO state edits are retained and recorded instead of overwritten.
- A crash after ADO comment creation or state patch resumes without duplicating
  either external action.
- Automated tests cover every milestone mapping, evidence validation, duplicate
  and stale handling, pending-order blocking, manual edits, outage/retry, and V5
  lifecycle non-interference.

## Qualification evidence

- Focused adapter, publisher, migration, and release tests pass with Postgres.
- The complete V5 suite passes with the story migration installed.
- A temporary `seerstone/agentic-mesh` User Story traversed
  `New → Active → Resolved → Closed` through the production adapter. Exact start
  replay returned the same durable milestone, while authoritative V5 work
  remained `active` at version 1. The temporary ADO item and database were
  permanently removed after verification.
