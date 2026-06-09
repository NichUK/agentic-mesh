# Configurable Sponsor Identity V0

Status: backlog

## Goal

Make sponsor identity and sponsor routing explicit project configuration rather than an implicit assumption that the original Teams sender is always the sponsor.

## Problem

Sponsor-facing questions, approval requests, blockers, and release decisions currently infer the sponsor from the inbound request context. That is a useful default, but it is not enough for enterprise use where sponsors may be a named person, a delegated product owner, a steering group, a channel, or a role-specific approval group.

The sender of agent-authored sponsor messages must always be the responsible Agentic Mesh actor, normally the relevant role bot or gateway bot. It must not appear to come from the sponsor.

## Scope

- Add project-level sponsor policy configuration.
- Support a default sponsor resolver for "original requester of the work".
- Support explicit named sponsors and sponsor groups.
- Support per-work-item sponsor override when intake creates or promotes a queue item.
- Record the resolved sponsor on work items, human gate requests, release decisions, and audit events.
- Route sponsor messages to the configured channel, DM, thread, or approval surface without changing the author identity of the agent message.
- Show the resolved sponsor and response route on work-item status pages.

## Acceptance Criteria

- A project can configure a sponsor policy in `project.yaml`.
- Existing projects continue to default to the original requester/source anchor.
- Sponsor questions and approvals display the resolved sponsor target and the agent sender separately.
- Thread replies from the configured sponsor route back to the active human gate request.
- Tests cover original requester, explicit sponsor, sponsor group, and per-work-item override resolution.
- Documentation explains the privacy, audit, and connector constraints for sponsor routing.

## Notes

The sponsor resolver should stay connector-neutral. Teams, Slack, GitHub Issues, Azure DevOps, email, and future gateway/API intake paths should all map to the same internal sponsor identity model.
