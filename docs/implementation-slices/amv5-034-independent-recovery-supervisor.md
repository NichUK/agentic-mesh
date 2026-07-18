# AMV5-034 — Independent recovery supervisor

## Outcome

Run the single durable recovery request created by AMV5-033 through a supervisor
that is independent of the normal role fleet and FastAPI control process. The
supervisor connects directly to Postgres, authorizes an external
`recovery:execute` identity, claims work with a renewable lease, and gives the
restricted recovery launcher the exact recorded goal and project scope.

The default execution limit is 120 minutes. A configurable provider-usage limit
is also supplied to the launcher. The supervisor records only safe summaries,
usage totals, profile provenance, and verification references; credential and
mount values remain external and are never written to Postgres or command
output.

## Boundaries

- Reuse the pending `recovery_requests` record and the existing 3/3/1 policy.
- Reuse external tool-profile resolution and require the profile to authorize
  `recovery-supervisor` on the independent launch path.
- Use one small launcher protocol. The concrete deployment, build, restart, Git,
  PR, and self-approval controls belong to AMV5-035.
- Keep the supervisor callable directly from the V5 CLI without FastAPI, normal
  role queues, the PM monitor, or a second broker.
- Reject recovery results on the general reliability API. A database trigger
  also requires every recovery-attempt row to match a durably reported
  supervisor run, so an internal caller cannot bypass the independent path.
- Pass credential and mount references from the resolved profile, never their
  values. The recovery job is explicitly scoped to its project.
- Make launcher invocation idempotent by stable run identifier. An expired lease
  may be reclaimed; a launcher must use that identifier to resume or replace the
  same external execution rather than start a duplicate repair.
- Persist a launcher result before applying it to the reliability policy. If the
  supervisor crashes between those operations, the next run replays the
  idempotent policy result.
- A launcher crash or expired lease is recoverable work, not verified failure.
  Only a structured failed result with a non-empty verification reference can
  make terminal error eligible.
- Keep the solution deliberately small: Postgres leasing, external
  authentication, one launcher interface, and no additional scheduler or
  orchestration framework.

## Acceptance criteria

- The standalone supervisor can claim and complete recovery while the control
  API is unavailable.
- A claim stores one stable run, an opaque lease, the exact goal source, project
  scope, resolved profile digest, 120-minute default time limit, and configured
  usage limit.
- Only an externally configured identity with `recovery:execute` permission for
  the project may claim or report recovery work.
- Normal routing, an unauthorized launcher, and a mutable goal all fail closed.
- The launcher receives the exact durable goal, deadline, usage cap, profile,
  credential references, and mount references without secret values.
- Expired claims are reclaimable using the same run identifier; current claims
  cannot be stolen; lease heartbeats extend only the matching active claim.
- A verified successful result resolves the incident and creates exactly one
  owner continuation. A verified failed result makes terminal error eligible.
- Missing verification, invalid usage, duplicate/conflicting results, and stale
  lease tokens are rejected without changing policy state. Reported usage above
  the pinned cap becomes a supervisor-verified failed result.
- A crash after durable result capture is recovered by idempotently applying the
  recorded result on the next supervisor run.
- Recovery request identity and exact goal are immutable in Postgres.

## Test plan

- Exercise authorization, external profile validation, claim, heartbeat,
  expiry/reclaim, success, failure, and idempotent replay against Postgres.
- Use a fake launcher to assert the complete job contract and that no secret
  value is present.
- Stop or bypass FastAPI and complete a request through the direct supervisor
  command path.
- Inject crashes before launch, after result capture, and after reliability
  application; verify one run, one attempt, and one continuation.
- Try normal-routing profiles, unauthorized launchers, goal mutation, stale
  leases, malformed results, missing verification, time/usage overruns, and
  conflicting replays.
- Verify migration install/upgrade, journal/audit evidence, CLI JSON redaction,
  existing retry-policy behaviour, and the full V5 test suite.
