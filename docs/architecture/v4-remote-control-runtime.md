# Agentic Mesh V4 Remote-Control Runtime

V4 replaces the V3 broker-wrapped worker model with Codex app-server role
containers. Each configured role instance runs Codex app-server in remote-control
mode and receives work through runtime-managed Postgres queues.

## Runtime Boundary

The V4 runtime owns infrastructure:

- Teams/API ingress
- Postgres message queues and operational state
- Codex app-server WebSocket connections
- safe-output tool recording
- dashboard/reporting
- hibernation and wake coordination
- OpenTelemetry hooks

The runtime does not make specialist project decisions. Role agents own
conversation, judgment, handoffs, consultations, work progression, and sponsor
communication.

## Role Agent Boundary

Each role instance has an external config folder containing an `AGENTS.md`
generated from shared instructions, role charter, organisation/project guidance,
RACI/governance, tool permissions, memory rules, and document-library rules.

### Review proportionality and convergence

The shared V4 standing prompt must prevent governance activity from becoming a
self-sustaining work stream. Before a role creates a consultation, revision, or
handoff, it must verify that the action directly advances the work item's
accepted outcome or resolves a material blocker. Incidental, speculative, or
implementation-detail findings that do not change acceptance criteria, a
credible security/compliance boundary, feasibility, or release correctness are
recorded as non-blocking follow-up and do not trigger another role cycle.

A reviewer may return the same material finding for up to three focused
correction-and-re-review loops. Each loop must stay tied to the overall accepted
outcome and use the minimum engineering necessary to resolve the finding,
without expanding into speculative improvements. If the finding remains
unresolved after the third correction and re-review, the accountable role must
stop the specialist loop and either disposition the deeper problem or ask the
sponsor for a decision. A fourth specialist bounce requires explicit sponsor
direction.
Newly discovered work outside the current objective belongs in a separate work
item and requires normal prioritisation; it must not be grafted onto an
incident merely because it was discovered there. Sponsor stop instructions
terminate the active review chain and must not create a replacement handoff.

Acceptance criteria for this prompt contract are:

- every materialized role `AGENTS.md` contains the relevance and
  proportionality check;
- minor or speculative findings are explicitly non-blocking and cannot require
  another handoff;
- the same material finding has a maximum of three focused
  correction-and-re-review loops before deeper-problem disposition or sponsor
  escalation;
- out-of-scope work requires a separate prioritized work item;
- sponsor stop instructions prohibit further artifact revisions and specialist
  handoffs for the stopped loop; and
- human-facing responses contain conclusions and evidence links rather than raw
  session logs, tool transcripts, or search dumps.

Role containers run:

```text
codex app-server --listen ws://0.0.0.0:{port}
```

The runtime connects over authenticated internal WebSocket and drives Codex with
`thread/start`, `thread/resume`, `turn/start`, `turn/steer`, `turn/interrupt`,
`thread/read`, and paginated turn/item APIs.

## Message Delivery

Inbound Teams or API messages are persisted before delivery. Normal messages are
queued when an agent is busy. Explicit steering messages may be delivered to the
active Codex turn with `turn/steer`.

The V4 dispatcher reads the Postgres queue, wakes the target role container with
Docker Compose when the role is not running, connects to that role's
authenticated app-server WebSocket, and delivers the message into the role's
existing thread or a new thread. If the app-server is still unavailable during
wake-up, the message returns to `queued` with a journal entry instead of being
lost.

V4 no longer supports SQLite for active runtime state. Local and dogfood
deployments use Postgres for message queues, role/session state, safe-output
records, artifacts, handoffs, memory, and reporting read models.

Conversational replies come from the Codex stream. Durable project effects still
require safe-output tools, including handoff, consult, queue/work creation,
document/artifact updates, approval requests, sponsor questions, memory updates,
release/deployment records, blockers, risks, and decisions.

### Human-wait notification invariant

A dashboard state is not a human notification. Before a role can move work to
`waiting_human`, `awaiting_human`, `awaiting_decision`, `human_review`, or
`blocked_on_human`, that same role must create an open Sponsor decision and
successfully deliver its decision card through Teams. The durable delivery
record must contain the Teams activity identifier.

Acceptance criteria are:

- a human-wait update without a delivered Sponsor decision is rejected without
  changing the work item;
- a pending or failed Teams attempt does not satisfy the gate;
- the blocking role, work item, open decision, and delivery receipt are bound
  together so another role's notification cannot satisfy the gate;
- successful updates record the notification identity in the safe-output audit
  payload; and
- delivery failure keeps work operationally visible and must never be reported
  as a successful sponsor notification.

The decision card itself must state the underlying problem, its practical
consequence, and what the recommended answer enables in short ordinary English.
Internal finding codes, work-item ids, stage labels, and governance references
belong in source references or the supporting-details link, not in the card's
title or question.

### Nonterminal continuity invariant

A role turn cannot complete while any tracked work item it touched remains
nonterminal without a live durable continuation. The completion gate accepts
exactly three outcomes:

- the overall work item is terminal;
- a handoff target message for that work item is queued or active; or
- the item is explicitly waiting for a human and an open Sponsor decision has
  a successful Teams delivery receipt.

Milestone labels such as `stage1_complete`, local commit completion, review
completion, or readiness for promotion are not terminal outcomes. If no valid
continuation exists, the turn becomes `completed_with_missing_output` and the
runtime immediately queues repair/escalation work rather than allowing the
fleet to become silently idle.

## Active Deployment

The V4 deployment profile no longer starts V3-only NATS, V3 supervisor, V3
role-service containers, or per-message `codex exec` worker paths. Historical
V2/V3 code remains in the repository until it is deliberately removed, but the
default CLI and linuxch release script target V4.
