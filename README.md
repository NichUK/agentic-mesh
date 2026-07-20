# Agentic Mesh

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

V5 is the only active runtime and product direction. It lives under
`src/agentic_mesh_v5` and must not import retired runtime modules. V4 is dead:
there is no migration, dual-run, or cutover path. Useful historical assets are
classified and ported or rewritten through explicit V5 stories rather than
coupling the runtimes.

The primary deployment platform is Linux Docker on the LinuxCH host. Project
manifests and project-owned deployment artifacts are canonical; Windows Docker
is used only for development and qualification.

## Current Runtime Direction

V5 development follows the ordered `AMV5-*` Azure DevOps backlog. The initial
bootstrap package can be checked without starting V4:

```powershell
python -m agentic_mesh_v5 --json boundary-check
python -m agentic_mesh_v5 --json status
python -m agentic_mesh_v5 --json resolve-config --config-root C:\Dev\agentic-mesh-config --package project-override/example-project@0.1.0
python -m agentic_mesh_v5 --json release-create --config-root C:\Dev\agentic-mesh-config --package project-override/example-project@0.1.0 --actor sponsor
python -m agentic_mesh_v5 --json release-status --config-root C:\Dev\agentic-mesh-config
$env:AGENTIC_MESH_V5_DATABASE_URL="postgresql://mesh:$env:DB_PASSWORD@localhost:5432/mesh"
python -m agentic_mesh_v5 --json database-migrate
python -m agentic_mesh_v5 api-serve
```

Independent recovery remains callable when that API process is unavailable.
`recovery-run-once` connects directly to Postgres, resolves the externally
pinned recovery tool profile, authenticates an external `recovery:execute`
token, and invokes one external launcher command without a shell. The command
receives only the exact durable goal, project scope, limits, and credential or
mount references; secret values remain outside runtime state and CLI output.
The default recovery limit is 120 minutes.

The V5 API exposes database-independent liveness at `/api/v1/health/live` and
schema-aware readiness at `/api/v1/health/ready`. It creates local
OpenTelemetry spans and metrics without requiring a collector. Set the standard
`OTEL_EXPORTER_OTLP_ENDPOINT`, or its traces/metrics-specific endpoint, to
enable OTLP/gRPC export; credentials and collector settings remain external.
The same authenticated boundary exposes the pinned external-flow operations:
start/read, obligation dispatch, document artifact verification, consultation
and accountable gate evidence, handoff-backed transition preparation/pickup,
and terminal completion. The acting role comes from the bearer principal, while
flow and `DocumentStore` adapters are supplied by project configuration rather
than request payloads. In production, the API materialises the OneDrive store
from the active immutable manifest and a refreshable, read-only external
credential root; Graph bearer values never enter environment variables or
worker containers. The AMV5-045 qualification drives a compact product,
development, QA, and release flow through these APIs and the sponsor CLI,
including API/worker/PM restart recovery and a reclaimed handoff lease.

V5 is described as an ordinary arm's-length project by
`examples/projects/agentic-mesh-v5/agentic-mesh/project.yaml`. The generic
`project-register` CLI pins that manifest, the active external configuration
revision, a digest-addressed running image, and disjoint install, state, and
workspace roots (all supplied as absolute paths). Project source work uses the isolated
worktree path; registration does not introduce a self-hosting execution mode.
Run `python -m agentic_mesh_v5 project-register --help` for the bootstrap
arguments. `runtime-upgrade` then verifies an exact clean commit, runs the
selected tests, builds and smoke-checks the V5-only image, deploys its content
digest through the project-owned Compose file, and either records candidate
health or restores the verified previous image. The operation is durable and
idempotent, and live source is never mounted into the running service. Run
`python -m agentic_mesh_v5 runtime-upgrade --help` for the explicit project,
source, test, image, and Compose arguments.
On a classic Docker engine that does not retain repository digests for local
builds, pass `--publish-image` with an authenticated registry repository; the
candidate tag is pushed before its immutable `repo@sha256` reference is
resolved. Publication is never implicit.

The V5 ADO adapter resolves its organization, project, and external credential
reference only from the active project manifest. It links the authoritative V5
work item to a numeric ADO ID and project-qualified URL, verifies
`System.TeamProject` on every response, and uses durable operation IDs plus a
read-before-patch check for safe update replay. ADO outages leave the V5 work
item unchanged and the external operation pending for pickup. The milestone
publisher sends only start, material handoff, blocker/recovery, pull request,
deployment, and acceptance updates—not the event stream. Each durable update
has one ordered sequence, a stable comment marker, typed evidence, and a next
action. Conditional state changes preserve manual ADO edits and require the
approved implementation/test or acceptance/owner-review evidence before moving
to `Resolved` or `Closed`.

The V5 Teams binding declares one distinct bot application id, display name,
and external credential reference for every logical role in the project
manifest. Role activation pins that application id, so every same-role worker
instance shares the role identity without sharing work-item context. The first
connector adapter maps inbound recipient ids to logical roles and checks bot
installation and send permission before outbound delivery. Missing installation,
revoked permission, credential failure, and connector failure are safe explicit
blockers; tokens never enter the manifest, database, image, delivery evidence,
or error text.

V5 Teams project routing uses only active manifest authority. Channel messages
require one exact tenant, Team, channel, and recipient bot match. Personal
messages are filtered through authenticated sender/project authorization: one
candidate routes, while multiple candidates return a structured question and
no selected project. Only an exact structured project choice can resume that
route; message text is never parsed for project or role selection. Approval
requests use the same durable gate and transactional outbox as the rest of V5.
The requesting role sends one idempotent personal Adaptive Card to each project
sponsor; authenticated callbacks are accepted only after durable dispatch and
delegate to the exactly-once sponsor coordinator. Approved, rejected, and
expired cards are updated safely. Selected structured progress checkpoints can
use the same outbox to send concise role-specific sponsor updates without
turning every live checkpoint into notification noise.

V5 database maintenance and native backup operations use the same external
`AGENTIC_MESH_V5_DATABASE_URL`. A backup briefly enters the durable write pause,
publishes a custom-format archive with its checksum manifest as the publication
marker, and resumes only if it initiated the pause. Restore accepts an empty
database and leaves it paused for explicit verification:

```powershell
python -m agentic_mesh_v5 --json database-backup --output C:\Backups\mesh.dump --actor operator --reason "scheduled backup"
python -m agentic_mesh_v5 --json database-restore --archive C:\Backups\mesh.dump
python -m agentic_mesh_v5 --json database-resume --actor operator --reason "restore verified"
```

V5 worker execution uses provider-neutral engine, thread, turn, event, usage,
completion, and safe-error contracts. The first adapter wraps the official
`openai-codex` SDK and its local stdio app-server; Codex JSON-RPC methods and
generated models remain inside that adapter. The SDK and V5 worker image share
one pinned Codex version, while credentials and `CODEX_HOME` remain external.
Named OAuth caches are supplied by the deployment and resolved into an external
`CODEX_HOME`; V5 never reads or copies `auth.json`. An official-SDK status probe
refreshes the existing login and reports only safe authentication state.
The warm-engine pool keys engines by project and role-instance identity, reuses
one engine across successive turns, and closes it only after a fatal engine
failure, explicit hibernation, or shutdown. Idle policy and autoscaling remain
separate control-plane decisions.
Persistent provider threads are bound by project, work item, logical role, and
conversation. A replacement or same-role worker instance resumes that recorded
thread; another project, work item, role, or conversation cannot inherit it.
Each binding pins the immutable effective configuration digest that created its
thread. A later activation cannot alter active work: callers must keep using the
pinned digest or perform an explicit, audited, idle-only reseed. Durable
operation claims serialize one conversation across same-role instances, and a
new thread's creator acquires that claim in the same database transaction. A
reseed creates a new generation and provider thread without copying old context.
Role workers publish progress through structured checkpoints rather than model
text. Each checkpoint carries explicit goal, step, completed action, activity,
blocker, next action, and a caller-supplied safe summary. Work-item sequence
preconditions reject stale writers, checkpoint ids make retries idempotent, and
common credential/private-reasoning patterns are rejected before persistence.
The existing read model and SSE path publish the stored fields directly; no
summarizer model or free-text parser is involved.
The V5 worker dispatch loop is intentionally introduced later by AMV5-029; when
added, it must use `ThreadAffinityCoordinator` rather than call provider thread
start/resume directly.

V5 keeps lifecycle judgement with role agents and reduces the runtime to
infrastructure: API ingress, Postgres-backed queues and state, warm Codex
workers, durable safe-output operations, hibernation/wake coordination, and
telemetry.

Useful development commands:

```powershell
pip install -e .[dev]
pytest -q
$env:AGENTIC_MESH_V5_DATABASE_URL="postgresql://mesh:password@localhost:5432/mesh"
agentic-mesh --json boundary-check
agentic-mesh --json database-status
agentic-mesh api-serve
```

The canonical LinuxCH deployment is project-owned and must provide Postgres,
external configuration, Codex OAuth, connector credentials, and project-scoped
Git credentials through external mounts or secret references. Secrets never
belong in Git, images, logs, prompts, or database records.

## Repository Layout

- `src/agentic_mesh_v5/`: active V5 runtime package
- `tests/test_v5_*.py`: V5 boundary and feature tests
- `config/v5/organization-config-repository.json`: external V5 package repository reference
- `examples/projects/agentic-mesh-v5/`: V5 arms-length project declaration
- `docs/architecture/v5/`: V5 design and classified historical asset evidence
- `docs/architecture/v4-remote-control-runtime.md`: retired V4 history
- `docs/architecture/v3-agent-owned-runtime.md`: previous V3 reset architecture
- `docs/architecture/v2-runtime-reset.md`: previous v2 reset plan and topology
  direction
- `docs/architecture/repository-topology.md`: source/runtime/project boundary
  guidance
- `docs/architecture/risk-register.md`: risks captured during dogfood
  recovery

## Standards-Informed Roles And Flows

Agentic Mesh adapts established enterprise and professional standards into
configurable role templates, project flows, gates, and document
accountabilities. The standards guide defaults; they do not lock every project
into one methodology.

- BMAD informs specialist AI agent roles, named workflows, role-specific
  capabilities, and artifact-driven handoffs.
- Scrum informs accountability boundaries, product ownership, developer
  planning ownership, flow transparency, and impediment handling.
- SFIA informs focused professional skill profiles.
- BABOK informs Business Analyst work such as stakeholder context and
  requirements lifecycle thinking.
- TOGAF informs a proportional enterprise architecture practice across
  Business, Data, Application, and Technology domains. Product Definition
  records architecture impact for every item; material or uncertain changes
  update the durable architecture portfolio and receive Enterprise Architect
  conformance review, while low-impact work records why detailed review is not
  required.
- V5 enforces this with version-pinned external flow obligations and immutable
  project-scoped evidence. Document path/eTag verification, consultation
  response or justified exception, and accountable review decisions must exist
  before their obligations are ready; routes use the recorded impact rather
  than caller input.
- Sponsor gates use the authenticated V5 API or `sponsor-decision` bootstrap
  CLI without requiring Teams or a dashboard. Decisions are project-authorized,
  audited, and resume one exact role continuation; rejection or timeout returns
  the pending decision to the Project Manager for correction.
- ISTQB and BDD practice inform QA planning and evidence handling.
- OWASP SAMM and NIST SSDF inform secure SDLC expectations and residual-risk
  recording.
- Prompt engineering practice informs prompt contracts, safe-output tool
  instructions, context boundaries, and behavioural regression scenarios for
  role agents.

## Product Positioning

Agentic Mesh is intended to be open core.

Open source core should remain useful on its own:

- V5 durable control plane and role-agent runtime
- Teams connector, document-library, safe-output, and Codex app-server adapter ports
- governance-aware role agents and RACI
- Postgres local backend
- Docker Compose deployment profile
- documentation framework primitives
- status/reporting dashboard
- starter role and flow templates
- prompt-engineering role support for agent prompt contracts and behavioural
  diagnosis

Commercial offerings may add supported enterprise deployments, SSO/RBAC,
managed storage/message backends, policy packs, compliance reporting, premium
support, and assisted operations.

The open source project should not be crippleware. Commercial features should
improve enterprise adoption, governance, reliability, and support rather than
locking away the basic agent network.

The Enterprise Architect is the accountable steward of the project portfolio
under `020-architecture/enterprise/`, including architecture principles and
vision, capability map, target operating model, domain architectures,
requirements, transition architectures, roadmap, conformance, exceptions, and
change history. TOGAF guides these defaults without forcing every work item
through the full ADM.
