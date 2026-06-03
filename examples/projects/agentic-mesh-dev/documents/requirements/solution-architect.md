# Solution Architect Worklist

Status: sponsor-editable adoption output

## Role View

The solution architecture should translate enterprise direction into concrete,
buildable product slices. The critical boundaries are role runtime, router,
control-plane, connectors, storage ports, worker adapters, and project build
outputs.

## Outstanding Work

- Define component contracts for router, control-plane, role runtime, connector
  ingress/egress, message store, state store, artifact store, journal, worker
  adapter, and deployment builder.
- Define the all-agents direct-work flow as a sequence diagram with no
  lifecycle handoff unless an agent chooses one.
- Define acknowledgement semantics: what gets acknowledged, where it is sent,
  who it is from, and how it is prevented from re-entering as work.
- Define project build model shared by Compose, Terraform, and Helm renderers.
- Define normalized worker request/result schema and tool protocol.
- Define document lifecycle contribution and owner-review events.
- Define connector event identity mapping, including mentions, DMs, channel
  posts, bot echoes, and Adaptive Card responses.
- Define error behavior: blocked, needs clarification, failed, retry,
  dead-letter, and optional handoff.

## Risks And Decisions

- Risk: Lifecycle machinery becomes an executive controller again. Mitigation:
  control-plane supervises health/lifecycle only; agents decide handoff based
  on loaded flow options.
- Risk: Connector-specific payloads leak into runtime. Mitigation: normalize
  messages at connector boundary and retain raw payload only as evidence.
- Decision needed: minimal v0 API between router and role runtime.

## Suggested Acceptance Criteria

- Main flows have sequence diagrams and event lists.
- Each component can be tested with fake adapters.
- Teams, Slack, and CLI can map to the same internal message/action contracts.
