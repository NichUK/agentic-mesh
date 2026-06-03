# Local Messaging v0

Status: accepted for current development slice

Date: 2026-06-02

## Product Goal

Give Agentic Mesh a small but real messaging spine for role work, handoffs, and
human response gates before building Teams, Slack, email, or control-plane UI
connectors.

The runtime already had file-backed role inboxes. This slice adds explicit
connector outbox messaging and typed message builders so human-facing requests
are durable, inspectable, journaled, and provider-neutral.

## Scope

- Keep role inbox queues as the delivery mechanism for role-agent work.
- Add file-backed connector outbox queues by logical channel.
- Add durable pending, claimed, and completed folders for connector messages.
- Add typed builders for `human_response.requested` and
  `human_response.received`.
- Emit human response request messages when a flow state contains a
  `human_response` gate.
- Include response template details from `config/response-types.yaml` in
  outbound connector messages so adapters can render an appropriate UI.
- Add a local CLI command for recording a human response back into the owning
  role's inbox.
- Record connector queue and human response request events in the journal.

## Acceptance Criteria

- Role inbox claim semantics still prevent duplicate claims.
- Connector outbox claim semantics prevent duplicate connector sends.
- A human response request message includes project, role, work item,
  lifecycle, gate, response type, prompt, completion criteria, and response
  template data.
- A human response received message can be routed to the role that owns the
  lifecycle state.
- When a role reaches a `human_response` gate, the runtime writes the normal
  artifact update, queues a connector message, records
  `human_response_requested`, and completes the claimed work as
  `waiting_for_human_response`.
- The implementation remains connector-neutral; Teams Adaptive Cards, Slack
  modals, email replies, local CLI prompts, and future UI forms are adapters
  over the same message contract.

## Non-Goals

- Real Teams, Slack, or email connector delivery.
- Full gate state persistence and re-evaluation.
- Timeout scheduling.
- Human response validation beyond loading configured response templates.
- Automatically resuming a paused lifecycle after the response is recorded.
