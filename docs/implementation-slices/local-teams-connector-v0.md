# Local Teams Connector v0

Status: accepted for current development slice

Date: 2026-06-02

## Product Goal

Prove the collaboration connector boundary with a local Teams-style adapter
before integrating Microsoft Graph.

The messaging slice created a connector outbox for durable human-facing
messages. This slice adds a local Microsoft Teams connector adapter that claims
those messages and renders Teams-shaped payloads into inspectable local state.

## Scope

- Add a connector adapter boundary.
- Add `LocalTeamsConnectorAdapter` as a local stand-in for a future Microsoft
  Teams Graph connector.
- Claim connector outbox messages by logical channel.
- Render `human_response.requested` messages into Adaptive Card JSON.
- Store rendered connector output under local runtime state.
- Add CLI commands for one-shot and looped local Teams connector processing.
- Add a `teams-connector` service to Docker Compose.
- Journal connector preparation events.

## Acceptance Criteria

- The local Teams connector can claim one pending connector outbox message.
- A second connector claim cannot process the same message.
- Human response requests render as Adaptive Card JSON.
- Choice response templates render as Teams submit actions.
- Connector processing records claim, preparation, and completion journal
  events.
- Docker Compose includes a `teams-connector` service.

## Non-Goals

- Sending messages to Microsoft Teams through Microsoft Graph.
- Creating Teams, channels, tabs, or webhooks.
- Receiving real Adaptive Card submit actions.
- Microsoft Graph OAuth, app registration, or permission handling.
- General Slack/email/web connector support.
