# Agentic Mesh V3 Agent-Owned Runtime

V3 is a hard reset toward self-contained role agents and a thinner runtime.
The runtime provides platform services; agents own work progression.

## Boundary

- Source repo: Agentic Mesh product code, templates, tests, schemas, and docs.
- Deployed runtime: installed artifact or image plus operational mounts.
- Organisation config repo: organisation defaults, identity, policy, and connector defaults.
- Project config repo: project role instances, RACI, channels, targets, and overrides.
- Document library: project memory and evidence, initially OneDrive mounted into Teams as Shared Files under `/documents`.

These paths must remain distinct unless a local-dev override is explicit.

## Runtime Responsibilities

- Start, stop, hibernate, and hydrate role-agent containers.
- Provide broker, connector, document-library, configuration, and telemetry services.
- Materialise configuration into agent-readable files.
- Maintain stakeholder reporting read models.
- Wake an agent when an inbox message arrives.

The runtime must not make product, architecture, implementation, QA, release,
or governance decisions. Those decisions belong to role agents operating under
RACI and sponsor instructions.

## Agent Responsibilities

Each role instance is a long-running service with:

- mounted role, organisation, project, RACI, and tool configuration
- broker-backed inbox/outbox
- DB-backed role memory
- access to source repositories granted by project config
- access to the document-library adapter
- prompt assembly from instructions, memory, governance, and current work

Agents must use tools for durable effects and must confirm meaningful action
after doing the work. If no action is appropriate, they must explicitly call a
no-op/status tool and explain why.

## Governance

Every work item carries a governance context: accountable role, responsible
roles, consulted roles, informed roles, sponsor decision points, and required
evidence before handoff, release, or closure.

Consultation is mandatory for RACI `C` roles unless the accountable role records
a governance exception. Informed roles receive concise notices when state
changes, major decisions happen, blockers appear, or releases occur.

Sponsor consultation is required for material changes to scope, priority,
acceptance criteria, user-visible behavior, release risk, cost, compliance,
security posture, or delivery commitment.

## Reporting

Initial reporting routes are:

- `/status`
- `/agents`
- `/work-item/{work-item-id}`
- `/artifact-viewer/{work-item-id}/{artifact-filename}`

The report plane reads projections and document-library metadata. It does not
own lifecycle decisions.

Local command smoke:

```powershell
agentic-mesh-v3 --db .tmp/v3.sqlite3 init-db
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev demo-slice --document-library-root .tmp/v3-documents
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev status-json
agentic-mesh-v3 --db .tmp/v3.sqlite3 --project-id agentic-mesh-dev serve --document-library-root .tmp/v3-documents
```

## Adapter Status

- Broker: V3 defines the broker port and local contract adapter; NATS JetStream
  is the first target adapter.
- Teams: V3 defines connector-neutral inbound messages, local Teams-shaped
  routing, and Graph-backed outbound message delivery with injectable transport.
- OneDrive: V3 defines the document-library port and Graph-backed OneDrive
  adapter with injectable transport. Work-item files live under
  `/documents/work-items/{work_item_id}`.
- Deployment: V3 defines command and no-deployment deployment targets so the
  Release Manager can execute a configured deployment or record a clear
  no-deployment disposition.

## Artifact Library

Work-item evidence lives under:

```text
/documents/work-items/{work_item_id}/
```

Every work item has an `index.md`. The root work-item index is:

```text
/documents/work-items/index.md
```

Durable architecture, product, engineering, QA, release, risk, decision, and
operations documents also live in the document library.
