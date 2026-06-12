# V2 Teams Connector Release Profile

Status: local dogfood release profile

Owner: release-manager

This profile defines the release evidence expected before the v2 Teams
connector is declared complete for a project. It separates local Compose
deployment from real tenant enablement so Release Manager can record either a
deployment or an explicit no-deployment disposition.

## Deployment Target

Local dogfood target:

- target id: `target-compose-dogfood`
- target type: `compose`
- project id: `agentic-mesh-dev`
- connector id: `teams-agentic-mesh-dev`
- Compose files:
  - `examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml`
  - optional Linux overlay:
    `examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml`
- runtime service: `v2-runtime`
- external base URL: `http://linuxch:8100`
- state database: `/mesh/project/state/v2/agentic-mesh-v2.sqlite3`

The Compose target must mount the project boundary at `/mesh/project`, run the
v2 runtime command, and keep runtime state outside the source image.

## Real Teams Tenant Evidence

Before a real Teams release, the release record must include evidence for:

- Teams app registration or Bot Framework app registration.
- Entra application owner and emergency owner.
- consent type for each permission: bot, delegated, application, or
  resource-specific.
- explicit approval for broad Graph permissions, if any are used.
- app installation in the configured project team.
- default project channel binding.
- any focus or private channel bindings.
- role identity bindings and display names.
- project sponsor/operator/release approver authority mapping.
- credential storage path by secret reference only, never secret value.
- external URL used in Teams cards and status links.
- retention policy for DMs, project channels, focus channels, deliveries, and
  receipts.
- disablement path for connector ingress and outbound delivery.

## Smoke Checks

Release Manager must record smoke evidence for:

- `/healthz` returns healthy.
- `/status.json` returns connector health, permission checks, delivery records,
  conversation events, relevance checks, context summaries, releases,
  deployment runs, and release evidence links.
- inbound DM creates a private conversation and role assignment.
- `status.reply` creates exactly one outbound delivery.
- project-channel context is captured without waking a role.
- role mention wakes only the mentioned role.
- team-wide relevance creates relevance assignments and only material replies
  should produce outbound messages.
- agent question creates a bound human-response thread/card.
- approval response validates authority.
- delivery failure records error class, retryability, and next action.
- permission failure fails closed with actionable connector attention.

## Rollback And Disablement

Rollback must not delete runtime state, conversation records, delivery records,
or audit events.

The first rollback action is:

1. Disable the deployment target in runtime state.
2. Stop connector ingress and outbound delivery for that target.
3. Redeploy the previous image or Compose configuration if needed.
4. Preserve the SQLite database and document library for audit.
5. Record the rollback reason and residual risk in the release record.

## Release Evidence Links

Every release must link evidence from these areas:

- product definition
- architecture or solution design
- security or permission review
- prompt or role operating contract
- engineering implementation log
- QA evidence
- release record

If any area is intentionally not applicable, Release Manager must record a
no-deployment or exception disposition rather than describing the work as fully
released.
