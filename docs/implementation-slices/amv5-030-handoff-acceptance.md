# AMV5-030 — Explicit handoff acceptance

## Outcome

V5 records a source offer and its routed target queue item in one Postgres
transaction. A target role instance must then claim the handoff with the lease
for that queue item and explicitly accept responsibility. The source queue item
cannot complete until the target has accepted.

## Boundaries

- Extend the existing Postgres handoff, role-queue, route and lease records. Do
  not add a second broker, acknowledgement service or workflow engine.
- A handoff is project-qualified and links its source queue item and lease to
  exactly one routed target queue item.
- Offering is idempotent. An exact retry returns the existing handoff; reuse of
  its key for different work fails closed.
- Claim and acceptance require the target queue item's lease token. A replaced
  expired lease may reclaim the handoff, while a second live lease cannot.
- Acceptance means the target has taken responsibility. It does not complete
  the target queue item or decide the work outcome.
- Existing direct route and queue operations remain available for bootstrap
  and operator use.

## Service targets

The durable record exposes these independently measurable targets:

1. target queue materialisation within 10 seconds of the offer;
2. target lease claim within 90 seconds of queue materialisation;
3. explicit target acceptance within 120 seconds of claim.

Pending milestones expose overdue state. Completed milestones retain their
actual latency and whether the target was met.

## Acceptance criteria

1. Offer and target queue materialisation commit together or both roll back.
2. Repeating the same offer, claim or acceptance after a lost response is safe
   and returns the one durable handoff.
3. A target role instance can claim only with the active lease for the exact
   routed item. Project, role, item and token mismatches fail closed.
4. If a target lease expires before acceptance, a later valid lease can reclaim
   and accept the same handoff without creating another target item.
5. Source queue completion is rejected until target acceptance and succeeds
   afterwards.
6. The API exposes authenticated offer, get, claim and accept operations and
   never returns lease tokens.
7. The 10/90/120-second latency and overdue results are deterministic and
   covered with boundary tests.
8. Migration, focused, full V5, V4-baseline, package/secret and clean-clone
   acceptance evidence is recorded before closure.

## Test plan

- Inject failure after target routing but before the handoff insert and prove
  transaction rollback leaves neither record.
- Retry every committed operation and run concurrent duplicate offers.
- Exercise wrong project, role, queue item, lease token, live competing lease,
  expired lease replacement and API authorization paths.
- Manipulate durable timestamps around 10, 90 and 120 seconds and assert final
  and pending target measurements.
- Attempt source completion before and after acceptance.
- Run migration upgrade/rollback checks, the complete V5 suite, the known V4
  baseline and clean-clone wheel verification.
