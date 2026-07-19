# AMV5-052 — Teams approval cards and progress updates

## Outcome

Deliver sponsor approval requests and selected safe progress summaries through
the requesting role's Teams identity, while retaining the existing V5 sponsor
gate as the sole durable decision authority.

## Scope and boundaries

- Opening a managed sponsor gate atomically writes its lifecycle event and one
  `teams.approval` transactional-outbox request containing the role, sponsors,
  safe summary, gate identity, and expiry.
- An idempotent Teams delivery adapter sends one personal Adaptive Card per
  sponsor through the requesting role's active bot identity. Per-sponsor
  operation ids make partial retry safe.
- Cards contain a concise proposed-change summary, expiry, rationale input, and
  approve/reject actions. They contain no credentials or private reasoning.
- A callback uses the authenticated Teams sender as the sponsor identity. It
  is accepted only when the matching project/gate/sponsor card was durably
  dispatched; card-supplied sponsor identity is never trusted.
- The callback delegates the decision to `SponsorApprovalCoordinator`, which
  already provides authorization, expiry, locking, idempotent replay, durable
  governance evidence, and exactly-once continuation routing.
- After a decision or observed expiry, all cards for that gate are updated
  through idempotent operations. A failed update does not roll back the durable
  decision and can be retried safely.
- Selected structured progress checkpoints can be queued once through the same
  event/outbox mechanism and delivered as role-specific sponsor DMs. Publishing
  is explicit to avoid turning every live checkpoint into notification noise.
- AMV5-052 does not add a second gate, approval, callback, delivery-state, or
  progress store. Existing event/outbox dispatch state is the durable delivery
  record.

## V4 reuse disposition

- Reuse V4's role-owned sponsor notification principle and Teams Adaptive Card
  interaction pattern.
- Rewrite delivery over V5 external role identities, transactional outbox,
  structured progress, and the V5 sponsor coordinator.
- Reject environment-held bot secrets, callback-authorized payload identities,
  free-form result parsing, and dashboard-only claims that a sponsor was
  notified.

## Acceptance criteria

- Every managed sponsor gate produces one durable Teams approval request in the
  same transaction as gate opening, and every sponsor receives the card from
  the requesting role identity.
- Partial delivery retries do not duplicate a sponsor card; missing identity,
  installation, permission, credential, or transport remains retryable and is
  not reported as delivered.
- Approve and reject callbacks require a dispatched card and authenticated
  authorized sponsor, and they change the durable gate and continuation once.
- Exact callback replay is harmless; conflicting or concurrent callbacks,
  foreign projects/sponsors, malformed actions, and expired cards fail closed.
- Card status updates are retryable after durable approve, reject, or expiry.
- Explicit progress publication is durable, idempotent, project-isolated,
  role-specific, safe-output-only, and delivered to configured project
  sponsors.
- Generic routing and approval semantics remain independent of Teams.
