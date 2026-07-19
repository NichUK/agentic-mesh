# Architecture

Agentic Mesh is a project-scoped mesh of role agents, collaboration
connectors, storage backends, worker/model adapters, and observable runtime
services.

V4 remains the deployed baseline during the V5 build. V5 is a clean package
boundary under `src/agentic_mesh_v5`; it cannot import V4 runtime modules.
Cross-version reuse occurs only through explicit asset classification and
porting stories so V5 does not inherit V4 execution mechanics accidentally.

The current architecture direction is recorded in:

- `docs/architecture/v3-agent-owned-runtime.md`
- `docs/architecture/agentic-mesh-design.md`
- `docs/architecture/decisions.md#adr-004---v3-agent-owned-runtime-reset`
- `docs/architecture/decisions.md#adr-001---agentic-mesh-enterprise-runtime-direction`
- `docs/architecture/risk-register.md`

## Direction Summary

Agentic Mesh replaces the OpenAgents-centered prototype direction with an
enterprise runtime designed around independently deployable, long-running role
agents. V3 sharpens this by making role agents own work progression while the
runtime provides platform services only.

Each role-agent instance runs in its own container with:

- role identity
- role configuration
- project override configuration
- standing instructions
- approved tools
- role-specific storage volume
- inbox, outbox, and journal
- OpenTelemetry service identity
- lifecycle policy for idle hibernation and automatic wake-up

Role definitions are separated into permanent role templates and
project-specific overrides. A company can reuse the same base roles across
many projects while adjusting local instructions, tool access, connectors,
write boundaries, and deployment scale per project.

Projects may run multiple instances of the same role where needed. The runtime
must treat role template, role instance, and project assignment as separate
concepts.

V5 registers every project, including Agentic Mesh itself, through the same
manifest and role-pack path. A durable runtime boundary pins the external
configuration revision and digest-addressed running image while keeping the
running install, runtime state, and external worktrees disjoint. Self-hosted
development is therefore ordinary project work, not a privileged mutation
path into the live runtime.

Idle role-agent instances can hibernate after a configurable grace period.
The control-plane wakes hibernated instances when new role work, DMs, mentions,
scheduled work, or manual operator action requires them. This keeps local and
cloud-native installations resource-efficient without losing durable queue
messages or role state.

Agentic Mesh is intended as an open source project with a commercial offering
on top. The open source core should remain useful on its own. Commercial
features can focus on enterprise support, advanced control-plane capabilities,
SSO/RBAC, managed cloud deployment, policy packs, compliance reporting, and
operational support.

## Core Boundaries

- Role agent: owns work progression, governance consultation, documentation,
  handoffs, stakeholder questions, implementation/review/release actions within
  its authority, and confirmations.
- Runtime platform: starts, stops, hibernates, hydrates, and observes role
  agents; provides broker, connector, document-library, configuration,
  reporting, and telemetry services.
- Worker adapter: executes model or CLI work through Codex, OpenAI, Anthropic,
  Claude Code, MiniMax, DeepSeek, or future providers. V5 exposes only neutral
  engine, thread, turn, event, completion, usage, and safe-error contracts. Its
  first implementation wraps the official Codex Python SDK's local stdio
  app-server; Codex wire methods and generated SDK types stop at the adapter.
  A provider-neutral pool keeps one engine warm per project/role-instance,
  serializes that instance's operations, and leaves idle/scale policy outside
  the provider lifecycle.
- Thread affinity: stores one globally unique provider thread for each
  project/work-item/logical-role/conversation key. Concrete same-role instances
  may resume it after hibernation or failure, but unrelated contexts fail
  closed rather than receiving a fresh or foreign thread. Each conversation
  pins its effective package digest and permits one durable active operation.
  Configuration activation never changes that pin; an explicit, idle-only,
  audited reseed advances the generation and starts a new thread.
- Progress checkpoint: appends project/work/role-instance-scoped goal, step,
  action, activity, blocker, next action, and safe summary fields under an
  idempotency id and expected sequence. Sensitive content fails before
  persistence; live views consume the structured record without model parsing.
- Failure policy: advances one durable work-item incident through three
  technical retries, three distinct Project Manager corrections, and one
  independent recovery request. Attempt/event history is append-only, and the
  lifecycle rejects terminal error until a failed recovery makes it eligible.
- Independent recovery: a standalone, direct-to-Postgres supervisor leases the
  exact recorded goal outside FastAPI and the normal fleet. It authorizes only
  external `recovery:execute` identities, pins the restricted tool profile,
  defaults to a 120-minute deadline, enforces a usage cap, and durably captures
  verified results before idempotently applying them to the failure policy. A
  bounded image runner creates one isolated repair branch, rejects unrelated
  changes, deploys and verifies the exact committed candidate, rolls back a
  failed deployment, and may create—but never approve or merge—a pull request.
- Message queue: provides durable inbox/outbox delivery. The active V4 runtime
  uses Postgres-backed queues; other queue backends remain future adapter
  options.
- Connector bridge: maps Teams, Slack, web, CLI, or other surfaces to the
  internal message/action model. It is transparent plumbing, not a manager.
- Control-plane: supervises topology, role-instance lifecycle, hibernation,
  wake-up, health checks, and configuration reloads.
- Message store: owns durable delivery, claims, retries, and dead letters.
- State store: owns project state, work item state, leases, cursors, and
  approvals.
- Artifact store: owns docs, decisions, stories, test evidence, release
  records, and generated artifacts.
- Document store: gives workers provider-neutral browse, metadata, read,
  create, and conditional-update operations. The V5 OneDrive adapter binds one
  instance to one manifest root, resolves bearer tokens only through external
  credential references, uses conflict-aware upload sessions for large files,
  and never forwards authorization to pre-authenticated content URLs.
- External flow and governance: pins a validated flow snapshot per work item,
  materializes only its declared state obligations, and routes state changes
  through accepted durable handoffs. Document versions, consultation responses,
  architecture classifications, owner/conformance decisions, and justified
  exceptions are immutable project-scoped evidence. The evidence layer never
  becomes a second lifecycle engine.
- Sponsor approval: composes the existing lifecycle gate/approval records,
  external-flow obligation, event/outbox, and role router atomically. API and
  bootstrap CLI decisions require the configured project sponsor; approval
  resumes the current flow owner once, while rejection or timeout returns the
  still-pending obligation to the Project Manager for correction.
- Autonomous kernel boundary: exposes the existing flow and governance stores
  through authenticated `/api/v1` operations without a second coordinator.
  The project supplies a pinned-flow resolver and scoped `DocumentStore`; the
  authenticated subject supplies role identity. The qualification flow proves
  product, Git-backed development, QA, sponsor approval, accepted handoffs,
  release evidence, restart takeover, and terminal completion without direct
  queue repair.
- Role pack: binds externally configured role templates to project queues, tool
  profiles, and prompts. Concrete instances share the logical role binding and
  role memory while retaining separate instance and work-item thread identity.
- Event journal: records an append-only audit/replay history regardless of
  queue backend.
- Project manifest: V5 loads the strict, Git-owned
  `agentic-mesh/project.yaml`, validates only external resource and credential
  references, and activates an immutable Postgres snapshot. Repository,
  document-root, Teams-channel, and ADO-project claims are exclusive unless an
  exact external grant authorizes sharing; project-scoped credential names are
  isolated by project.
- Git workspace: each mutating work item pins its project-manifest digest and
  exact base commits, then receives deterministic external worktrees for its
  selected repositories. Postgres records branch/path/revision evidence and
  supports crash pickup; cleanup removes only registered clean worktrees and
  never resets live source, deletes branches, or discards dirty user work.
- Runtime release: one durable project coordinator verifies an exact clean Git
  commit, configured tests, a V5-only image smoke probe, content digest, and
  database compatibility range. A project-owned deployment adapter observes
  drift, deploys only the immutable image, commits the registered boundary only
  after health, and restores the prior compatible image on failure. Restarted
  operations reconcile from the observed image and their Postgres attempt;
  neither the build nor deployment path mounts the source checkout.
- Shared memory: V5 stores concise, source-linked accelerators at project-role,
  project, and organization-role scope. Logical role instances share these
  records, but provider threads never enter the memory model. Every write must
  cite a current authoritative source; reads reverify it and visibly retain,
  but do not serve, stale or removed memory. Optimistic versions and immutable
  revisions prevent silent concurrent replacement. A deterministic write
  policy rejects secrets, redacts supported personal data, and permits
  organization scope only with positive organization-source evidence and no
  registered project identifier; uncertainty stays project-local.
