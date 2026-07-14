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

A reviewer may return the same finding for correction once. If it remains
unresolved after that correction and re-review, the accountable role must
either defer it with an explicit risk disposition or ask the sponsor for a
decision. A third specialist bounce requires explicit sponsor direction.
Newly discovered work outside the current objective belongs in a separate work
item and requires normal prioritisation; it must not be grafted onto an
incident merely because it was discovered there. Sponsor stop instructions
terminate the active review chain and must not create a replacement handoff.

Acceptance criteria for this prompt contract are:

- every materialized role `AGENTS.md` contains the relevance and
  proportionality check;
- minor or speculative findings are explicitly non-blocking and cannot require
  another handoff;
- the same finding has a maximum of one correction-and-re-review return before
  defer-or-escalate handling;
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

## Active Deployment

The V4 deployment profile no longer starts V3-only NATS, V3 supervisor, V3
role-service containers, or per-message `codex exec` worker paths. Historical
V2/V3 code remains in the repository until it is deliberately removed, but the
default CLI and linuxch release script target V4.
