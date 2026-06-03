# SDLC Teams Smoke Test

Status: bot-authored lifecycle completed with public HTTPS ingress caveat

Date: 2026-06-02

## Test Objective

Exercise the configured Agentic Mesh SDLC flow with role-agent containers
running on `linuxch`, handoff messages posted into Microsoft Teams, and a human
release response returned into the runtime.

## Current Teams UI Click Test Work Item

```text
slice-human-click-lifecycle-20260602215328
```

Initial enqueue result:

```json
{
  "message_id": "msg-177c65e8b495459fb14081e3e932a1db",
  "correlation_id": "corr-9684348158524454923b7c8f6e1ae2b9"
}
```

Runtime location:

```text
nich@10.0.0.65:/home/nich/agentic-mesh
```

Connector mode:

```text
teams-bot-connector
```

Temporary public ingress:

```text
https://tradition-matched-scientists-functions.trycloudflare.com/api/messages
```

This endpoint was created with a Cloudflare quick tunnel from `linuxch` to
`http://127.0.0.1:3978`.

The release approval was completed by clicking the Teams Adaptive Card in the
`approvals` channel.

## Previous Public-HTTPS Bot Test Work Item

```text
slice-public-card-lifecycle-20260602214816
```

Initial enqueue result:

```json
{
  "message_id": "msg-60aa4fecfa3648ee96be7f2531616483",
  "correlation_id": "corr-c9bfd15948d14c5387f5c64d0a7b744b"
}
```

Runtime location:

```text
nich@10.0.0.65:/home/nich/agentic-mesh
```

Connector mode:

```text
teams-bot-connector
```

Temporary public ingress:

```text
https://tradition-matched-scientists-functions.trycloudflare.com/api/messages
```

## Previous Bot-Authored Test Work Item

```text
slice-bot-lifecycle-20260602212801
```

Initial enqueue result:

```json
{
  "message_id": "msg-fd9bb168f6c447a7b96e5545bed9318e",
  "correlation_id": "corr-0cdbcf95fd994326a865a138d1bd5249"
}
```

Runtime location:

```text
nich@10.0.0.65:/home/nich/agentic-mesh
```

## Previous Delegated-Graph Test Work Item

```text
slice-teams-lifecycle-20260602210842
```

Initial enqueue result:

```json
{
  "message_id": "msg-46de141be74d4d7fb26c6ef0f312579a",
  "correlation_id": "corr-a756fea49431463a986fe56b7279180b"
}
```

Runtime location:

```text
nich@10.0.0.65:/home/nich/agentic-mesh
```

The delegated-Graph work item proved container lifecycle and delegated
Microsoft Graph channel posting. The bot-authored work items prove per-role
Teams bot outbound posting.

## Container Evidence

The Docker Compose stack was running on `linuxch` with 19 services, including:

- role-agent containers for all configured roles
- two `engineering` containers
- `router`
- `control-plane`
- `teams-connector`
- `teams-bot-listener`
- `otel-collector`

Listener health checks passed:

```text
http://127.0.0.1:3978/healthz
http://10.0.0.65:3978/healthz
```

## Lifecycle Evidence

The runtime journal recorded 13 agent runs for
`slice-human-click-lifecycle-20260602215328`:

| Role | Lifecycle state |
| --- | --- |
| `business-analyst` | `business_analysis` |
| `product-manager` | `product_definition` |
| `ux-designer` | `experience_design` |
| `enterprise-architect` | `enterprise_alignment` |
| `solution-architect` | `solution_design` |
| `security-architect` | `security_review` |
| `platform-engineer` | `platform_readiness` |
| `engineering` | `implementation` |
| `qa-engineer` | `quality_review` |
| `technical-writer` | `documentation_readiness` |
| `delivery-manager` | `delivery_readiness` |
| `release-manager` | `release_review` |
| `release-manager` | `release_review` after human response |

Final release completion status:

```text
completed_after_human_response
```

Human response evidence:

```json
{
  "gate_id": "release_decision_response",
  "response_request_id": "human-response-0c4148fdd45d4ce7b8781c4aa24126cd",
  "response_value": "approved",
  "responder": "Nicholas Overend"
}
```

Journal event counts for the bot-authored run:

| Event | Count |
| --- | ---: |
| `message_accepted` | 13 |
| `work_claimed` | 13 |
| `agent_run_started` | 13 |
| `documentation_updated` | 13 |
| `handoff_emitted` | 11 |
| `connector_message_queued` | 12 |
| `teams_bot_message_sent` | 12 |
| `teams_bot_message_failed` | 0 |
| `human_response_requested` | 1 |
| `human_response_received_from_teams` | 1 |
| `human_response_recorded` | 1 |

## Teams Evidence

The `teams-connector` container used the Bot Framework Teams connector path to
post channel messages as the configured role bot identities. The runtime journal
recorded 12 `teams_bot_message_sent` events and zero
`teams_bot_message_failed` events.

Teams search and channel reads found the bot-authored work item in these
`dev-team` channels:

| Channel | Visible Teams sender | Activity id |
| --- | --- | --- |
| `product` | `AM-Business Analyst` | `1780433615533` |
| `ux` | `AM-Product Manager` | `1780433622919` |
| `enterprise-architecture` | `AM-UX Designer` | `1780433621578` |
| `solution-architecture` | `AM-Enterprise Architect` | `1780433622379` |
| `security` | `AM-Solution Architect` | `1780433630538` |
| `platform` | `AM-Security Architect` | `1780433629311` |
| `engineering` | `AM-Platform Engineer` | `1780433628756` |
| `qa` | `AM-Engineering` | `1780433629914` |
| `technical-writing` | `AM-QA Engineer` | `1780433637065` |
| `delivery` | `AM-Technical Writer` | `1780433636449` |
| `release` | `AM-Delivery Manager` | `1780433643029` |
| `approvals` | `AM-Release Manager` | `1780433649116` |

The approval message was sent as an Adaptive Card attachment. Microsoft Graph
channel reads show this card-only message with `content: null`, author
`AM-Release Manager`, and message id `1780433649116`.

The approval response was submitted by clicking the Teams Adaptive Card. The
listener recorded:

```json
{
  "event_type": "human_response_received_from_teams",
  "responder": "Nicholas Overend",
  "timestamp": "2026-06-03T07:56:00.745179+00:00"
}
```

The release manager then recorded the response and completed the work item with
status `completed_after_human_response` at
`2026-06-03T07:56:04.426350+00:00`.

## Adaptive Card Update Verification

Teams delivered the approval button click as a legacy Adaptive Card
`Action.Submit` message activity. Returning an Adaptive Card response to that
activity only caused Teams to show its stock footer, "Your response was sent to
the app"; it did not replace the original card.

The listener now also calls Bot Framework `updateActivity` against the original
channel message. On 2026-06-03, work item
`slice-card-update-lifecycle-20260603091415` verified this path:

- `AM-Release Manager` posted approval card message `1780474497910`.
- The user clicked `Approve` in Teams.
- The listener recorded `human_response_received_from_teams` with responder
  `Nicholas Overend`.
- The listener updated the original Teams message and recorded
  `teams_bot_card_updated` with `updated_activity_id` `1780474497910`.
- The release manager completed the work item with status
  `completed_after_human_response`.

The replacement card keeps the selected decision visible as a single disabled
button and removes the unselected response buttons.

## Caveats

This is now a successful container-runtime, role-bot outbound Teams posting,
Adaptive Card posting, human Teams UI click, and public HTTPS listener test.
Remaining hardening caveats:

- `vpn.nixnet.com` was not reachable on ports `80`, `443`, or `3978` from the
  Codex machine during the test, so a temporary Cloudflare quick tunnel was
  used.
- Azure Bot resources were temporarily updated to the Cloudflare tunnel
  endpoint for verification. This is not a stable production endpoint.
- Bot Framework JWT validation is still required before exposing the listener
  for production-like inbound bot traffic.

## Next Verification Target

The next test should prove:

1. `https://vpn.nixnet.com/api/messages` routes to the `teams-bot-listener`
   container.
2. Azure Bot resources are updated from the placeholder endpoint to that public
   endpoint.
3. The listener validates Bot Framework JWTs and rejects unsigned activities
   outside controlled local tests.
