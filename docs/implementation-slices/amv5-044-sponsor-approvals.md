# AMV5-044 — Sponsor approvals through API and CLI

## Outcome

Complete externally declared sponsor gates through the authenticated control API or bootstrap CLI,
then resume the exact flow owner once on approval or route rejection/timeout to the Project Manager.

## Boundary

- Reuse the existing lifecycle `gates` and `approvals` records, external-flow obligation,
  transactional router, event journal, outbox, API authentication, and control client.
- A coordinator composes those records atomically; it is not another approval or workflow store.
- The current project sponsors are the only eligible decision makers. API project scope alone does
  not confer sponsor authority.
- An approval satisfies the current sponsor flow obligation and routes its current owner. Rejection
  and timeout leave the flow obligation pending and route the Project Manager for correction.
- Stable request fingerprints make exact duplicate decisions return their committed result without
  another queue item, event, or state transition. A conflicting duplicate is rejected.
- Timeout is an explicit durable outcome for the sponsor request, not terminal project failure.
- Teams cards and dashboard controls are later adapters over the same API.

## Acceptance criteria

- Product, prompt, and release sponsor gates can use the same generic external-flow mechanism.
- Authenticated API and `sponsor-decision` CLI commands support approve and reject without a UI.
- Only a configured sponsor for the exact project/gate can decide.
- Approval, rejection, timeout, duplicate, concurrent, audit, and cross-project cases are tested.
- Exactly one continuation is routed: current flow owner after approval, Project Manager after
  rejection or timeout.
