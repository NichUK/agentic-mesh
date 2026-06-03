# Teams Bot Identities

Status: draft operations note

Date: 2026-06-02

## Decision

Agentic Mesh uses one Microsoft Teams bot identity per role for the dogfood
`dev-team` project.

Role instances remain Agentic Mesh runtime identities. For example,
`engineering.1` and `engineering.2` share the Engineering Teams bot identity,
but they remain separate runtime workers, queues, journals, and telemetry
services.

## Current Dogfood Team

Team:

- name: `dev-team`
- id: `37a58a51-c42b-417b-8733-608afda205fc`

Existing and created channels:

- `all-agents`
- `approvals`
- `business-analysis`
- `product`
- `ux`
- `enterprise-architecture`
- `solution-architecture`
- `security`
- `platform`
- `engineering`
- `qa`
- `technical-writing`
- `delivery`
- `research`
- `release`

The project config maps these channels in
`examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml`.

## Provisioned Azure Resources

Provisioning status: Entra app registrations, service principals, Azure Bot
resources, Teams bot channels, Key Vault secret references, Teams app catalog
packages, and `dev-team` app installations have been created.

Azure subscription:

- name: `Seerstone Azure`
- tenant: `564b667c-5b1a-4bbc-bb43-918b0b765a9b`

Resource group:

- `agentic-mesh-dev`

Key Vault:

- `amdevbotskv564b`

Previous placeholder messaging endpoint:

- `https://agentic-mesh-dev-bot-placeholder.azurewebsites.net/api/messages`

Temporary verification endpoint:

- `https://tradition-matched-scientists-functions.trycloudflare.com/api/messages`

Dogfood listener endpoint target:

- `https://vpn.nixnet.com/api/messages`

The Azure Bot resources were temporarily updated to the Cloudflare quick tunnel
endpoint for public HTTPS verification. They should be updated to the dogfood
listener endpoint once `vpn.nixnet.com` routes to the listener container on
`linuxch`.

## Bot Identity Model

Each role has a Teams app/bot registration:

| Role | Teams bot display name | Azure Bot resource |
| --- | --- | --- |
| `business-analyst` | `AM-Business Analyst` | `am-business-analyst` |
| `product-manager` | `AM-Product Manager` | `am-product-manager` |
| `ux-designer` | `AM-UX Designer` | `am-ux-designer` |
| `enterprise-architect` | `AM-Enterprise Architect` | `am-enterprise-architect` |
| `solution-architect` | `AM-Solution Architect` | `am-solution-architect` |
| `security-architect` | `AM-Security Architect` | `am-security-architect` |
| `platform-engineer` | `AM-Platform Engineer` | `am-platform-engineer` |
| `engineering` | `AM-Engineering` | `am-engineering` |
| `qa-engineer` | `AM-QA Engineer` | `am-qa-engineer` |
| `technical-writer` | `AM-Technical Writer` | `am-technical-writer` |
| `delivery-manager` | `AM-Delivery Manager` | `am-delivery-manager` |
| `research-analyst` | `AM-Research Analyst` | `am-research-analyst` |
| `release-manager` | `AM-Release Manager` | `am-release-manager` |

The `AM-*` prefix comes from organization naming defaults:

```yaml
naming_defaults:
  brand_prefix: AM
  bot_display_name_template: "{brand_prefix}-{role_display_name}"
```

The current app registrations keep explicit display names in project config
because the bots already exist in Azure and Teams. New generated bot identities
should use the naming defaults unless a connector-specific override is needed.

Project config stores only references:

- `bot_id_ref`
- `secret_ref`

The actual Microsoft App ID, app password/client secret, certificate, or
managed identity details must live in the configured secret provider.

Generated Teams app packages:

```text
build/teams-apps/business-analyst.zip
build/teams-apps/product-manager.zip
build/teams-apps/ux-designer.zip
build/teams-apps/enterprise-architect.zip
build/teams-apps/solution-architect.zip
build/teams-apps/security-architect.zip
build/teams-apps/platform-engineer.zip
build/teams-apps/engineering.zip
build/teams-apps/qa-engineer.zip
build/teams-apps/technical-writer.zip
build/teams-apps/delivery-manager.zip
build/teams-apps/research-analyst.zip
build/teams-apps/release-manager.zip
```

These packages are generated artifacts and live under ignored `build/` output.

Published Teams app catalog ids:

| Teams app | Catalog id |
| --- | --- |
| `AM-Business Analyst` | `49ea4eba-bc8e-4252-bd5c-6a3dd6d53177` |
| `AM-Delivery Manager` | `53a369b0-d8b4-4f61-9d16-e90b4b510886` |
| `AM-Engineering` | `d519f1ec-0fef-44fc-9565-ec3a0c8e79c9` |
| `AM-Enterprise Architect` | `bcb7c2de-8544-4276-a7ea-a0398185289d` |
| `AM-Platform Engineer` | `81d2999a-8e74-451e-96e4-010c1e61f549` |
| `AM-Product Manager` | `67488b2c-ae6c-4610-a291-ce9be825556e` |
| `AM-QA Engineer` | `a64619a7-5b12-4c9d-b271-71623440ec56` |
| `AM-Release Manager` | `eee4e732-df23-46b7-aa97-6e1ba1707dc4` |
| `AM-Research Analyst` | `0c9459c2-b925-4396-ad4c-afbd39838dbf` |
| `AM-Security Architect` | `04af26fa-66a7-48b1-9775-30abac68d750` |
| `AM-Solution Architect` | `a5d2b139-b66b-4f20-a49f-e56176321f59` |
| `AM-Technical Writer` | `d9060a82-58c3-4ed1-8d18-3c1b527db081` |
| `AM-UX Designer` | `72795c4b-84de-4ea3-8451-2a4eec8d970b` |

Graph verification confirmed these `AM-*` apps are installed in `dev-team`.

## Bot Framework Outbound Verification

On 2026-06-02, all 13 role bot app registrations were given Entra service
principals so they can request Bot Framework tokens in the Seerstone tenant.
The `linuxch` runtime stores each role bot app id and secret under ignored
runtime state:

```text
/home/nich/agentic-mesh/examples/projects/agentic-mesh-dev/state/secrets
```

The Docker `teams-connector` service now uses:

```text
python -m agentic_mesh.cli teams-bot-connector-loop --connector teams --channel all --secret-root /mesh/project/state/secrets --poll-seconds 5
```

The SDLC smoke work items `slice-bot-lifecycle-20260602212801`,
`slice-public-card-lifecycle-20260602214816`, and
`slice-human-click-lifecycle-20260602215328` verified proactive Bot Framework
channel posting with visible Teams senders such as `AM-Business Analyst`,
`AM-Engineering`, and `AM-Release Manager`.

The later runs also verified that `human_response.requested` can be posted as
an Adaptive Card by `AM-Release Manager`. Graph channel reads show the
card-only message with `content: null`, which is expected for this
attachment-only activity shape. The human-click run verified that clicking the
card in Teams reaches the `teams-bot-listener`, records
`human_response.received`, and completes the release-review gate.

Teams sends these approval clicks as legacy Adaptive Card `Action.Submit`
message activities. The listener therefore cannot rely on the HTTP response to
replace the visible card. It returns an Adaptive Card response for newer invoke
flows, adds `msTeams.feedback.hide` to submit actions, and also calls Bot
Framework `updateActivity` against the original channel activity. The replacement
approval card removes unselected buttons and keeps the selected decision as a
single disabled button. The runtime journals this as `teams_bot_card_updated`.

Current limitation: inbound Teams activities still need the permanent public
`vpn.nixnet.com` route and Bot Framework JWT validation.

## Channel Message Intake

The Teams bot listener also normalizes ordinary Teams channel `message`
activities from mapped project channels into Agentic Mesh work.

For the dogfood project, a delivered message from `all-agents` is routed to the
flow's sponsor-initiated default intake:

- message type: `sponsor_intake.requested`
- target lifecycle state: `business_analysis`
- target role: `business-analyst`
- default work item type: `spike`

The generated work payload preserves Teams activity id, conversation id,
channel id, sender id/name, source channel, and the raw activity path.

If a Teams channel post does not appear in the listener journal, the missing
piece is delivery from Teams to the bot, not role-agent processing. The next
connector slice should add Graph channel read/subscription support for channel
posts and mentions that Teams does not deliver as bot activities.

## Graph Installer Path

Azure CLI can create Entra app registrations and Azure Bot resources, but its
first-party login client cannot request the delegated
`AppCatalog.ReadWrite.All` scope required to publish Teams app packages.

A tenant-local installer app registration was created for this setup:

- display name: `Agentic Mesh Teams App Installer`
- app id: `9c96d713-3ae4-4da9-998a-1a4a758f2763`
- secret ref: `amdevbotskv564b/teams-graph-installer-secret`

It has application permissions for catalog read/install automation and
delegated permissions for the publish path:

- application: `AppCatalog.ReadWrite.All`
- application: `TeamsAppInstallation.ReadWriteForTeam.All`
- delegated: `AppCatalog.ReadWrite.All`
- delegated: `TeamsAppInstallation.ReadWriteForTeam`

Publishing Teams app packages to the app catalog was completed with delegated
device-code authentication because Microsoft Graph requires a user-backed token
for that operation. Team installation was then completed through Graph for the
`dev-team` team.

The generated manifests initially included `packageName`, which is not valid in
Teams manifest schema `1.17`; it was removed from the generated build artifacts
before publishing.

## Setup Steps

For each role bot:

1. Create or register a Teams bot identity using Microsoft Teams Developer
   Portal, Teams Toolkit, Azure Bot Service, or an equivalent automated
   provisioning path.
2. Configure one HTTPS messaging endpoint for the bot.
3. Enable the Teams channel for the bot registration.
4. Package the bot in a Teams app manifest with the correct bot id, scopes, and
   display name.
5. Install the app into the `dev-team` Team.
6. Store the bot app id and secret in the configured secret provider using the
   refs from project config.

Microsoft Teams sends bot activities to the configured messaging endpoint.
The dogfood connector currently sends proactive channel messages by creating a
channel conversation against the configured Team and channel ids. A production
connector should also capture installation/conversation events so it can support
thread replies, personal scope messages, and richer conversation continuity.

## Why Not Users

Teams users are Entra ID users and usually imply licensing, MFA/credential
management, user lifecycle, and audit ownership. Bot identities are the
Teams-native path for agent-like participants.

## References

- Microsoft Teams bots overview:
  https://learn.microsoft.com/en-us/microsoftteams/platform/resources/bot-v3/bots-overview
- Microsoft Teams conversational bot capabilities:
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/build-conversational-capability
