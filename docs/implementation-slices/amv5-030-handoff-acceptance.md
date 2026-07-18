# AMV5-030 — Explicit handoff acceptance

## Outcome

V5 records a source offer and its routed target queue item in one Postgres transaction. A target
role instance must claim the handoff with that item's lease and explicitly accept responsibility.
The source queue item cannot complete until the target has accepted.

## Boundaries

- Extend the existing Postgres handoff, role-queue, route and lease records. Do not add a second
  broker, acknowledgement service or workflow engine.
- A handoff links its project-qualified source queue item and lease to one routed target item.
- Offering is idempotent. An exact retry returns the handoff; reuse for different work fails closed.
- Claim and acceptance require the target item's lease token. A replacement expired lease may
  reclaim the handoff, while a second live lease cannot.
- Acceptance takes responsibility; it does not complete the target item or decide the outcome.
- Existing direct route and queue operations remain available for bootstrap and operator use.

## Service targets

The durable record exposes these independently measurable targets:

1. target queue materialisation within 10 seconds of the offer;
2. target lease claim within 90 seconds of queue materialisation;
3. explicit target acceptance within 120 seconds of claim.

Pending milestones expose overdue state. Completed milestones retain latency and target outcome.

## Acceptance criteria

1. Offer and target queue materialisation commit together or both roll back.
2. Repeating an offer, claim or acceptance after a lost response returns the one durable handoff.
3. A target can claim only with the active lease for the exact routed item. Project, role, item and
   token mismatches fail closed.
4. After target lease expiry, a later valid lease can reclaim and accept without another item.
5. Source queue completion is rejected until target acceptance and succeeds afterwards.
6. Authenticated API offer, get, claim and accept operations never return lease tokens.
7. The 10/90/120-second latency and overdue results have deterministic boundary tests.
8. Migration, focused, full V5, V4-baseline, package/secret and clean-clone evidence is recorded.

## Test plan

- Inject failure after routing but before handoff insert and prove rollback leaves neither record.
- Retry every committed operation and run concurrent duplicate offers.
- Exercise wrong project, role, item, token, live competing lease, expired replacement and API auth.
- Manipulate durable timestamps around 10, 90 and 120 seconds and assert final/pending measurements.
- Attempt source completion before and after acceptance.
- Run migration upgrade/rollback, full V5, known V4 baseline and clean-clone wheel checks.
