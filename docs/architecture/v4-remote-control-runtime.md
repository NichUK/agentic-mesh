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
