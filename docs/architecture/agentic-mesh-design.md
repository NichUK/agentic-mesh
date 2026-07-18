# Agentic Mesh Design

Status: draft design

Date: 2026-06-02

Related decision: `ADR-001`

## Working Name

The working name is **Agentic Mesh**.

The name describes the intended product shape: a mesh of role agents,
collaboration connectors, storage backends, tool adapters, deployment
profiles, and observable runtime services. It deliberately avoids tying the
product to software development teams only. The same architecture should be
able to support broader enterprise agent networks later.

Rejected working names:

- `Enterprise Agent Network`: accurate but too generic and
  infrastructure-heavy.
- `DevTeam Mesh`: good for the initial use case, but too narrow for the
  intended product expansion.
- `AgentMesh`: concise but less distinctive.
- `TeamFabric`: good enterprise feel, but less explicit about role-agent
  execution.

## Purpose

Agentic Mesh replaces the OpenAgents-centered prototype with an enterprise
runtime designed around long-running role agents.

The product goal is not to build another general agent framework. The goal is
to provide a deployable, inspectable, observable, project-scoped agent network
where each role owns work, communicates through enterprise collaboration
channels, updates documentation as it works, and hands work to other roles
without requiring a central executive controller.

The first product use case remains an AI software delivery team, but the
architecture must not be limited to development roles.

## Design Principles

- Role agents are long-running workers with their own identity, storage,
  configuration, instructions, queue, and tool boundary.
- Role templates use functional role titles, not hard-coded named personas.
- Permanent role templates are governed centrally and updated rarely.
- Projects can override role instructions, tools, write boundaries,
  connectors, and scaling without mutating the permanent role template.
- A project may run multiple instances of the same role.
- Agents communicate through durable messages and visible collaboration
  channels, not through hidden controller-only state.
- Humans interact through enterprise collaboration tools first, starting with
  Microsoft Teams.
- The runtime is connector-agnostic so Slack and other collaboration surfaces
  can be added later.
- Storage is pluggable. Local filesystem storage is the default developer
  backend; cloud-native storage and messaging backends are supported behind the
  same ports.
- Git is the durable source for configuration, documentation, decisions,
  stories, evidence, and committed artifacts.
- Runtime queues are append-only and inspectable by default, but may use cloud
  queue services for enterprise reliability.
- Idle role-agent instances can hibernate after a configurable grace period and
  be restarted automatically when new work, DMs, or connector events arrive.
- Every deployment emits OpenTelemetry logs, traces, and metrics.
- Model providers are adapters. The agent runtime must support Codex, OpenAI,
  Anthropic, Claude Code, MiniMax, DeepSeek, and future providers without
  changing product-level workflow semantics.

## Runtime Topology

Each role-agent instance runs in its own container. The container includes the
role runtime, worker adapter, approved tools, and access to a role-specific
mounted volume.

Example local topology:

```text
agentic-mesh/                    # system/runtime repository
  config/
    organization.yaml
    roles/
      product-manager.yaml
      solution-architect.yaml
      enterprise-architect.yaml
      engineering.yaml
      qa-engineer.yaml
    flows/
      sdlc.yaml
    schemas/
      project.schema.json
  examples/
    projects/
      agentic-mesh-dev/
        agentic-mesh/project.yaml
        deploy/
          compose/
            docker-compose.yml
            docker-compose.linuxch.yml
        state/                   # ignored local runtime state

agentic-mesh-projects/
  quantauma/                      # project repository/workspace
    agentic-mesh/project.yaml
    deploy/
      compose/
      terraform/
      helm/
    state/
    src/
    tests/
    docs/
    evidence/
    releases/
```

Core containers:

- `router`: routes messages, handoffs, direct messages, action responses, and
  connector events. It is not an executive agent and does not decide how work
  progresses.
- `control-plane`: supervises project topology, role-instance lifecycle,
  hibernation, wake-up, health checks, and configuration reloads. In the open
  source core this can be a lightweight local service; commercial offerings may
  provide richer enterprise control-plane capabilities.
- `agent.<project>.<role>.<instance>`: one container per role-agent instance.
- `teams-connector`: maps Microsoft Teams teams, channels, messages, DMs, and
  Adaptive Card actions into internal messages and back out.
- `otel-collector`: receives logs, traces, and metrics from every runtime
  component.
- `config-ui`: future optional container that edits and validates config files,
  then commits approved config changes.

Optional enterprise containers or services:

- cloud queue adapters
- cloud storage adapters
- secret provider adapters
- policy enforcement adapters
- Git synchronization or audit commit service

## Role Template And Project Override Model

Agentic Mesh separates stable role definition from project-specific behavior.

### Role Template

A role template is a permanent or semi-permanent definition maintained by the
organization or product owner.

It defines:

- role id
- role purpose
- baseline standing instructions
- default input and output contracts
- default handoff rules
- default documentation obligations
- required capabilities and tool-use constraints
- default telemetry labels
- default security posture

Role templates do not define `default_tools`. V3 tool access is generated at
runtime from the role charter, role authority, project policy, configured
connectors, and the V3 tool catalog. This keeps permanent role templates from
drifting into stale tool inventories while still giving agents explicit,
auditable tool instructions in their materialized prompt.

Abbreviated example:

```yaml
role_id: engineering
version: 1
purpose: Implement approved work and hand evidence to QA.
role_profile: >
  Engineering owns implementation quality for approved work and records clear
  evidence before handing to QA.
accountabilities:
  - Implement approved source, tests, and engineering documentation.
  - Record implementation evidence and handoff requirements.
decision_rights:
  owns:
    - Source-level implementation choices inside approved scope.
  advises:
    - Feasibility, technical risk, testing strategy, and deployment impact.
  escalates:
    - Scope, architecture, security, or release questions outside engineering authority.
standing_instructions:
  - Only implement work that has passed product, architecture, and delivery readiness.
  - Update implementation notes as work progresses.
  - Hand completed work to qa-engineer with evidence.
quality_bar:
  - Changes are focused, tested, and backed by reproducible evidence.
capabilities:
  schema_version: role-capability-profile-v0
  capabilities:
    - capability_id: source.read
      category: logical_tool
      requirement: required
      display_name: Source read
    - capability_id: tests.run
      category: logical_tool
      requirement: required
      display_name: Test runner
documentation_obligations:
  - docs/engineering/implementation-log.md
handoff_targets:
  - qa-engineer
```

### Organization Defaults

Organization defaults apply across projects without changing role templates.

They define:

- global language and locale for conversations, documents, generated
  artifacts, and handoffs
- authentication method catalog for workers, connectors, and tool adapters
- approved providers
- default worker adapter
- enterprise policy constraints
- secret provider references
- telemetry destination
- default storage backend
- default connector configuration
- documentation standards
- conversation standards
- handoff standards
- security defaults

### Project Override

A project override specializes a role for one project.

It may define:

- project-specific instructions
- project-specific constraints
- allowed repositories or worktrees
- allowed write paths
- project documentation paths
- connector channel mappings
- storage backend selection
- worker/model selection
- worker authentication binding by method id and secret or mount reference
- document accountabilities and owner-review expectations
- instance count
- tool narrowing or expansion subject to policy

Example:

```yaml
project_id: quantauma
workspace:
  root: .
  default_repository: quantauma
  repositories:
    quantauma:
      type: git
      path: .
      default_branch: main
roles:
  engineering:
    template: engineering
    instances: 2
    worker:
      adapter: codex-cli
      model: codex
      auth:
        method: codex_access_token
        secret_ref: codex-quantauma-engineering-token
    instructions:
      - Follow Quantauma front-office/back-office boundary decisions.
      - Do not change production deployment files without release approval.
    write_paths:
      - src/**
      - tests/**
      - docs/engineering/**
    channels:
      primary: engineering
      handoff_inbox: engineering
```

`workspace.root` is resolved under `AGENTIC_MESH_WORKSPACE_ROOT` unless it is
absolute. Role `write_paths` and flow `artifact_path` values are relative to
that effective project workspace. This is the boundary that lets role agents
do real work in source, test, documentation, evidence, and release files while
the runtime image remains reusable and configuration/state stay external.

For dogfood projects nested inside a system repository, set `workspace.root` to
the project folder and configure the system repository as a repository path
inside that workspace. Project artifacts then stay under the project folder
while agents can still inspect or work against the system repository.

### Role Instance

A role instance is a concrete running worker.

It has:

- stable instance id
- role template reference
- project assignment
- container identity
- volume path
- queue lease identity
- telemetry service name
- optional specialization

Example instance ids:

- `quantauma.engineering.1`
- `quantauma.engineering.2`
- `quantauma.product-manager.1`

Multi-instancing is how a project scales a role. It must not require cloning or
renaming the role template. Work claiming, leases, and handoff routing must
support competing consumers for the same role.

### Naming And Telemetry Identity

Organization defaults define runtime naming and branding policy. The open-source
dogfood default is:

```yaml
naming_defaults:
  brand_prefix: AM
  service_name_template: "{brand_prefix}.{team_slug}.{role_id}.{ordinal}"
  bot_display_name_template: "{brand_prefix}-{role_display_name}"
  resource_namespace: agentic-mesh
```

Role-agent telemetry service names use the configured template. For the
`dev-team` dogfood project, Engineering instance 1 emits:

```text
service.name = AM.dev-team.engineering.1
```

The `team_slug` is derived from the primary collaboration connector team name.
If a project has no collaboration team configured, it falls back to
`project_id`. This keeps similarly named roles in different enterprise teams
distinguishable in observability tools, for example
`AM.dev-team.engineering.1` versus `AM.accounting.engineering.1`.

Microsoft Teams bot display names should also follow the naming policy unless a
connector-specific override is required. Current dogfood app registrations keep
explicit `AM-*` display names, but those names are generated from the same
branding convention.

## Agent Lifecycle And Hibernation

Agentic Mesh should support queue-aware role-agent hibernation.

The goal is to reduce cost and idle resource usage, especially in cloud-native
installations, without making role agents feel unavailable to humans or other
agents.

Lifecycle states:

- `configured`: role instance exists in project configuration but is not
  running.
- `starting`: control-plane is creating or waking the container.
- `idle`: container is running and no active work is claimed.
- `active`: container has claimed work, is handling a DM, or is processing a
  connector event.
- `draining`: container will finish active work but should not claim new work.
- `hibernating`: container is being stopped after the idle grace period.
- `hibernated`: role instance is stopped but can be restarted automatically.
- `failed`: role instance failed health checks or startup.

Wake triggers:

- new role work item enters the role inbox
- direct message targets the role or a specific role instance
- collaboration connector event mentions or addresses the role
- scheduled task becomes due
- manual operator wake
- project startup policy requires warm agents

Hibernation rules:

- A role instance may hibernate only when it has no active claim, no active DM
  exchange, no pending tool call, and no unflushed journal entry.
- Hibernation uses a configurable grace period, for example 3 to 10 minutes.
- The control-plane records the last idle time, hibernation reason, and wake
  reason in the event journal.
- A hibernated instance keeps its role-instance id, queue lease identity,
  mounted volume, memory files, and journal.
- Hibernation must not lose messages. New messages remain in the message store
  and wake the instance or another eligible instance.
- Multi-instance roles can hibernate some instances while keeping a minimum
  warm pool where configured.

V5 materializes the lifecycle policy in Postgres while keeping its source
configuration external. A project pre-registers a bounded pool of stable
role-instance identities. Reconciliation wakes an instance immediately when a
zero-sized role receives ready work, adds one instance when busy capacity has
left ready work waiting for the configured threshold (60 seconds by default),
and hibernates surplus specialists after the idle grace period (300 seconds by
default). The Project Manager retains a minimum of one warm instance.

The desired `starting` or `hibernating` transition and its action id are
durable before the orchestration adapter is called. Deployment adapters apply
an action idempotently, so a control-plane restart repeats rather than loses
the operation. An unreleased work lease, active thread operation, or pending
instance-correlated outbox record prevents hibernation. Prompt-pinned thread
affinity and queued work remain durable outside the worker container.

Configuration example:

```yaml
roles:
  engineering:
    template: engineering
    instances: 3
    lifecycle:
      min_warm_instances: 1
      idle_grace_seconds: 300
      wake_on:
        - inbox_message
        - direct_message
        - mention
        - scheduled_work
```

The control-plane is allowed to start, stop, restart, and health-check
containers. It must not decide product, architecture, implementation, QA, or
release outcomes.

Open source control-plane scope:

- read Git-backed config
- start/stop containers locally
- watch local queues
- wake hibernated agents
- expose basic status
- emit OTEL telemetry

Commercial control-plane opportunities:

- enterprise SSO and RBAC
- fleet dashboards
- policy enforcement
- multi-project topology management
- cloud autoscaling integrations
- hosted status and audit views
- advanced cost controls
- SLA-backed support workflows

## Role Agent Runtime

The runtime owns work lifecycle behavior. The model or CLI behind the agent is
replaceable.

The runtime is responsible for:

- loading role template, organization defaults, project override, and instance
  configuration
- polling or subscribing to the role inbox
- claiming work atomically
- ensuring one active claim per instance unless explicitly configured
- loading project, slice, documentation, and prior-message context
- enforcing role, organization, project, and slice write policy
- invoking the selected worker adapter
- recording status, blockers, handoffs, approvals, documentation updates, and
  artifacts
- emitting OpenTelemetry spans, logs, and metrics
- writing to the role journal and shared documentation areas

The runtime must not assume a particular model provider, collaboration product,
or storage backend.

## Worker Adapter Abstraction

Worker providers execute role work behind a provider-neutral lifecycle. V5
models a warm engine containing resumable threads, with one streamed turn at a
time per thread. Provider events are translated into stable progress, item,
plan, usage, error, and completion events before they reach product code.

```text
WorkerProvider
  open() -> WorkerEngine
WorkerEngine
  start_thread(ThreadRequest) -> WorkerThread
  resume_thread(thread_id, ThreadRequest) -> WorkerThread
WorkerThread
  start_turn(TurnRequest) -> WorkerTurn
WorkerTurn
  events() -> ProviderEvent stream
  interrupt()
```

The first V5 implementation is the local Codex provider. It uses the official,
version-pinned `openai-codex` Python SDK to control a local app-server over its
stable interface and reuses the existing external Codex authentication. Codex
JSON-RPC notification shapes and SDK exceptions remain inside the adapter;
queues, projects, flow rules, and workers depend only on the neutral contract.
An engine stays warm across turns and is closed on hibernation or failure.
The warm-engine pool is keyed by both project and role-instance identity. It
serializes operations within one instance while allowing other instances to
run concurrently. Successful and failed operations refresh safe monotonic idle
activity; only successful operations increment the use count. Transport or
protocol failure evicts the engine, while explicit hibernation and shutdown
wait for an active operation before closing it. Queue wait thresholds and
autoscaling remain control-plane policy rather than provider behavior.

Provider thread affinity is durable and keyed by project, work item, logical
role, and conversation. It is not keyed by concrete instance, so a replacement
instance of the same role can resume the logical agent's context. First-thread
creation is serialized with a Postgres advisory transaction lock, and a
provider/thread id can belong to only one affinity key globally. The executing
instance must match the key's project and role through both application checks
and a composite database foreign key. A missing or rejected recorded thread is
an error; the runtime must not silently start a context-free replacement. A new
binding and its first turn run within the same warm-engine checkout because
Codex does not guarantee an untouched empty thread is resumable after shutdown.
AMV5-025 provides this production coordinator before a V5 worker dispatch loop
exists. The later routing/worker loop must invoke the coordinator; automated
source-boundary checks reject any other product module that starts or resumes a
provider thread directly.

Every affinity also pins the 64-character digest of the effective immutable
configuration release used to seed the thread. Global activation or rollback
does not rewrite existing affinities, and every operation must present the
recorded digest. A durable active-operation token allows only one same-context
operation across all role instances. The token is released after both success
and handled in-process failure; a process crash remains visibly fail-closed for
later recovery supervision. First binding or post-reseed thread creation and
the creator's operation claim commit in one transaction, so another instance
cannot take ownership of an untouched provider thread between those steps.
Bind/claim and reseed use the same per-affinity advisory lock, making their
ordering deterministic. If claim release also fails while another operation
error is unwinding, the original error remains authoritative and receives only
a safe release-failure note.

Changing an existing conversation requires a controlled reseed with the
expected current digest, a different target digest, actor, and reason. Reseed is
rejected while an operation is active. Its append-only record preserves the old
provider/thread identifiers and both digests, then the affinity enters
`pending_seed` with no provider thread. The next real operation creates the new
generation's thread. Prompt text, private reasoning, and credentials are never
copied into the reseed record.

Role progress uses the existing durable `progress` and live-read boundaries.
Each write is an explicit checkpoint with a project-unique idempotency id and
an expected previous sequence. The store locks the work item, verifies that the
concrete role instance belongs to the same project, and appends the next
sequence in the same transaction that emits its live-read event. Replaying the
same id and payload returns the original record; stale or conflicting writers
fail without a partial row or sequence gap.

Goal, step, completed action, current activity, blocker, next action, and safe
summary remain separate fields. The safe summary is supplied by the role and
stored directly. Bounded validation rejects common credential material,
private keys, credential-bearing connection strings, and explicit private
reasoning tags before database access. V5 does not parse model prose or invoke
another model to construct operational progress.

Planned and possible providers remain adapter choices rather than product
semantics:

- `codex-cli`
- `openai-api`
- `anthropic-api`
- `claude-code`
- `deepseek-api`
- `minimax-api`
- `manual-human`

The runtime must define internal event and tool protocols rather than leaking
provider formats into the product. Adapters map those protocols to
provider-specific capabilities where available. Authentication, capacity, and
transport failures are classified without copying raw provider messages, which
may contain secrets or prompt content, into durable runtime state.

Worker authentication is configured separately from role semantics. A project
selects an auth method from the system catalog in `config/auth-methods.yaml`
and provides only `secret_ref`, `mount_ref`, or non-secret hints. See
`docs/architecture/authentication.md`.

## Human Response Gates

Human gates are structured response contracts, not just approval requests. A
flow gate can require a human or external authority to provide a response
before handoff continues.

Reusable response type templates live in `config/response-types.yaml`. Stock
templates include approve/not approve, yes/no, number, money, single-line text,
multiline text, document reference, URL, and document-or-URL responses. Flow
gates reference these templates with `response_type` and define gate-specific
completion criteria.

Connector adapters such as Teams Adaptive Cards, Slack modals, email replies,
local CLI prompts, and future control-plane forms should render the same
response contract into their own UX. The core runtime stores and evaluates the
normalized response, not the connector-specific payload. See
`docs/architecture/human-response-gates.md`.

## Document Lifecycle

Projects can declare document accountabilities. The accountable owner ensures a
document exists, is complete, and remains correct, but other roles may
contribute. Contributions should emit document lifecycle events and can trigger
owner review before downstream handoff gates pass. See
`docs/architecture/document-lifecycle.md`.

Normalized request fields:

- agent id, role id, and role instance id
- role template version
- organization defaults version
- project override version
- role instructions and standing operating contract
- work item purpose, source, project, slice, and priority
- prior messages and handoff context
- allowed tools and write boundaries
- documentation requirements
- output contract

Normalized result fields:

- status: `completed`, `blocked`, `needs_clarification`, `failed`
- human-readable message
- handoffs
- documentation updates
- artifacts
- approval requests
- tool calls and evidence
- provider usage, cost, latency, and failure metadata

## Storage Ports

Storage is abstracted into separate ports so local and cloud-native deployments
can vary independently.

### Message Store

Purpose:

- role inboxes
- role outboxes
- handoffs
- direct messages
- action responses
- retries and dead-letter records

Local backend:

- append-only JSONL or maildir-style folders
- atomic claim files or leases
- simple inspectable payloads

Azure backend:

- Azure Service Bus topics/subscriptions for delivery
- Azure Table Storage or Blob Storage for payload metadata and event journal

AWS backend:

- SQS/SNS or EventBridge for delivery
- DynamoDB or S3 for metadata and event journal

### State Store

Purpose:

- project state
- work item state
- agent status
- claim leases
- connector cursors
- approval state

Local backend:

- Postgres for active runtime state, queues, and read models
- YAML or JSON files only for static configuration or exported snapshots

Azure backend:

- Azure Table Storage initially
- Cosmos DB only when richer query, scale, or partitioning requirements justify
  it

AWS backend:

- DynamoDB
- S3 metadata where appropriate

### Artifact Store

Purpose:

- documentation
- product decisions
- architecture decisions
- enterprise architecture records
- feature stories
- BDD scenarios
- test evidence
- release records
- generated reports

The project document library includes a durable Enterprise Architecture
portfolio under `020-architecture/enterprise/`. Enterprise Architect is
accountable for principles, vision, capability and target-operating-model
views, Business/Data/Application/Technology architectures, requirements,
gaps, transitions, roadmap, conformance, exceptions, and change history.
Work-item dossiers retain only slice-specific impact, decisions, and evidence.

Architecture governance is proportional. Product Definition records a
structured impact classification for every item. Material or uncertain changes
route through Enterprise Alignment and require conformance or an approved
exception before implementation/release; low-impact changes record why the
full architecture stage is unnecessary.

Local backend:

- Git workspace and filesystem

Azure backend:

- Git repository plus Azure Blob Storage for larger or binary artifacts

AWS backend:

- Git repository plus S3 for larger or binary artifacts

### Event Journal

Regardless of queue backend, every deployment must keep an append-only event
journal.

The journal is the audit and replay source. A queue service such as Azure
Service Bus may deliver messages, but it is not the historical record by
itself.

The journal records:

- message accepted
- message routed
- work claimed
- agent run started
- tool invoked
- documentation updated
- handoff emitted
- approval requested
- action response received
- work completed, blocked, failed, or retried

### Durable Routing And Explicit Handoff Acceptance

V5 role routing uses the project-qualified Postgres role queues and durable
leases. A handoff is stronger than a routed message: its source offer and target
queue item commit atomically, the target instance claims it with the lease for
that exact item, and the target explicitly accepts responsibility. The source
queue item cannot complete while one of its outbound handoffs is unaccepted.

Offer, claim and acceptance are retry-safe. An expired target lease may be
replaced by a later valid lease without creating another handoff or target item.
The durable record measures queue materialisation within 10 seconds, target
claim within 90 seconds, and acceptance within 120 seconds of claim. These are
observable service targets, not extra workflow components.

### Global Project Manager Continuation

One logical Project Manager monitor holds a durable singleton lease and sweeps
all active projects. Process restarts can take over an expired lease without
changing the logical identity. Pending sponsor gates are intentional stops;
structured ambiguity opens a sponsor-clarification gate; and nonterminal work
without a ready, delayed, or validly leased continuation is routed once to its
project's Project Manager queue. An unavailable PM route is persisted as an
operator-visible blocker rather than being treated as a successful sweep.

## Service Bus Position

Azure Service Bus is a strong enterprise delivery backend, but it should not be
the default product assumption.

Advantages:

- reliable delivery
- competing consumers
- retries
- dead-letter queues
- enterprise operations familiarity
- scaling multiple instances of the same role

Trade-offs:

- less transparent than local append-only queues
- harder to debug without an accompanying event journal
- not a complete audit or replay record by itself
- cloud dependency for local development

Decision:

- local development defaults to file-backed append-only queues
- enterprise Azure deployments may use Service Bus for delivery
- all deployments retain an inspectable append-only event journal

## Collaboration Connector Abstraction

The collaboration connector is broader than a notifier. It is the user-facing
bridge between enterprise collaboration tools and the internal message model.

```text
CollaborationConnector
  receive_message()
  send_message()
  send_direct_message()
  send_action_request()
  update_action_request()
  map_identity()
  map_channel()
  map_thread()
```

Initial connector:

- Microsoft Teams

Future connectors:

- Slack
- web console
- email
- CLI

The internal message model must preserve enough metadata for every connector:

- source connector
- external team/workspace id
- external channel id
- thread id
- message id
- sender identity
- mentions
- attachments
- action payloads
- permalink when available

## Microsoft Teams First UI

Teams is the first enterprise UI.

Mapping:

- one Microsoft Team per project
- one channel per role surface
- optional `all-agents` channel for cross-role status
- optional `approvals` channel for approval cards
- DMs for direct sponsor or role conversations where appropriate

Example:

```text
Team: Quantauma Project
  #product
  #architecture
  #enterprise-architecture
  #delivery
  #engineering
  #qa
  #release
  #approvals
  #all-agents
```

Teams approval and action UX should use Adaptive Cards. The internal action
model must remain generic so Slack can map the same action to Block Kit later.

## Git And Configuration

Git owns:

- permanent role templates
- organization defaults
- project overrides
- role instance declarations
- connector configuration templates
- storage backend configuration templates
- documentation
- decisions
- stories
- test plans
- test evidence
- release notes
- committed audit snapshots

Git should not be used as the only live queue implementation for high-frequency
runtime messages. Runtime queues are file-backed or cloud-backed, then
summarized, snapshotted, or committed where useful.

The future configuration UI must edit config files, validate them, and commit
changes. It should not become a hidden database-backed configuration authority.

Agentic Mesh should distinguish the system repository from project
repositories. The system repository owns runtime code, default configuration,
role templates, reusable flow templates, configuration schemas, examples, and
product documentation.
Real projects should live in separate repositories or workspaces that carry
project overlays, documentation, evidence, deployment outputs, runtime state
locations, connector/team bindings, and secret references by name. Project
build outputs such as Compose, Terraform, and Helm belong under the project
folder, not the system repository root. See
`docs/architecture/repository-topology.md` and
`docs/implementation-slices/project-build-outputs-v0.md`.

## Flow Templates And Schemas

Flow is project configuration layered over role instructions. Role templates
describe durable role behavior, while a flow template describes lifecycle
states, accountable owner roles, handoff targets, artifacts, and gates for a
class of work.

Flows are collaboration graphs, not only forward pipelines. A state can define
`handoffs` for lifecycle progression and `consults` for bounded requests to
other roles. Consults may point backwards, forwards, or sideways in the flow and
do not complete the current lifecycle state. They give an agent an explicit
list of roles it may ask for help, context, review, research, or evidence.

Project flows can also define `sponsor_initiated_work`. This policy tells every
role what to do when a sponsor asks that role a question or request that
creates work. The agent must capture or request a tracked work item, choose the
appropriate start state or default intake state, and run the item through
consult routes, handoffs, gates, evidence, and closure. See
`docs/architecture/flow-collaboration.md`.

The system repository may provide stock flow templates under `config/flows/`.
The first template is `config/flows/sdlc.yaml`, which describes the dogfood
software-delivery lifecycle for slices, features, and spikes. Projects can
reference a template and apply project-specific overrides without copying the
whole graph into every project YAML file.

Project configuration schemas live under `config/schemas/`. The initial
`project.schema.json` documents the expected shape for project overlays,
including roles, worker/auth bindings, document accountabilities, and either an
inline flow or a referenced flow template. `response-types.schema.json`
documents the reusable response template catalog.

The human-readable companion for the project schema is
`docs/configuration/project-configuration.md`. Use it when designing or
reviewing a project YAML file.

## Open Source And Commercial Model

Agentic Mesh should be an open source project with a commercially sustainable
offering layered on top.

Open source core should include:

- role template and project override model
- role-agent runtime
- worker adapter interface
- local file-backed storage adapters
- append-only event journal
- Docker Compose deployment profile
- basic control-plane for local lifecycle and hibernation
- OpenTelemetry instrumentation
- collaboration connector interface
- basic Microsoft Teams connector where licensing and platform constraints
  allow
- starter role packs, including a BMAD-inspired software delivery pack
  expressed as functional role templates rather than named agents

Commercial offerings may include:

- supported enterprise deployment packages
- managed or assisted Azure/AWS deployment
- advanced control-plane UI
- SSO integration
- enterprise RBAC and policy packs
- premium Teams integration features
- compliance and audit reporting
- cost and usage dashboards
- managed cloud storage/message backends
- priority support
- training, onboarding, and role-template customization

Commercial features should enhance enterprise adoption, supportability,
governance, and operations. The basic ability to run a useful local/project
agent mesh should remain open source.

## Observability

Every component emits OpenTelemetry.

Required span boundaries:

- connector receive
- message accepted
- message routed
- queue claim
- agent run
- worker adapter invocation
- tool invocation
- documentation write
- handoff emit
- approval request
- action response
- retry or dead-letter
- agent hibernate
- agent wake
- control-plane health check

Required metric examples:

- queue depth by role and role instance
- work claim latency
- agent run duration
- token/cost by provider, role, and project
- handoff count
- blocker count
- retry count
- dead-letter count
- connector send failure count
- hibernated instances by project and role
- wake latency
- idle runtime saved where estimable

Required log qualities:

- correlation id
- project id
- slice id where applicable
- role id
- role instance id
- connector id
- external message id where applicable
- lifecycle state
- redacted sensitive values

## Deployment Profiles

### Local Developer Profile

- Docker Compose
- file-backed queues and state
- Git workspace artifacts
- local OTEL collector
- optional local Teams connector emulator or real Teams connector

### Azure Enterprise Profile

- containers on Azure Container Apps, AKS, or App Service for Containers
- Azure Service Bus for delivery
- Azure Table Storage for state
- Azure Blob Storage for large artifacts
- Git repository for config and documentation
- OpenTelemetry Collector to Azure Monitor / Application Insights
- Key Vault for secrets
- Managed Identity where possible

### AWS Enterprise Profile

- ECS, EKS, or equivalent container runtime
- SQS/SNS/EventBridge for delivery
- DynamoDB for state
- S3 for large artifacts
- Git repository for config and documentation
- OpenTelemetry Collector to the enterprise observability stack
- Secrets Manager or Parameter Store for secrets

## Security And Isolation

Each agent container should have:

- role-specific identity
- role-instance-specific storage volume
- least-privilege tool access
- least-privilege connector permissions
- least-privilege cloud storage permissions
- explicit write policy over shared artifacts
- independent telemetry identity

Secrets must not be stored in role config, project overrides, or Git. Config
files reference secret names or provider paths, not secret values.

## Future Flow Catalogs

Project flow overlays let each project define its own lifecycle graph. Agentic
Mesh should eventually provide optional stock flow catalogs based on common
enterprise delivery and operating patterns.

This should be treated as research-backed product work, not an assumption in
the core runtime. When a Research Analyst role is added, it should own an
initial research task to identify candidate stock flows, document the
enterprise patterns they come from, and propose which should become starter
flow templates.

Potential research areas:

- software delivery lifecycle variants
- enterprise architecture review flows
- security and compliance review flows
- procurement and vendor assessment flows
- data governance and analytics delivery flows
- incident, support, and operations flows
- change advisory and release governance flows

Stock flows should remain optional templates. Projects must still be able to
override lifecycle states, owners, handoffs, artifacts, and parallel work item
types.

## Open Questions

- Should the router be one container per project or a shared multi-project
  service?
- Should connector routing and runtime supervision be separate services, or one
  deployable module until multi-project scale requires separation?
- Should each role-agent instance support multiple concurrent claims, or should
  horizontal scale be expressed only by adding more instances of the role?
- What is the right repository boundary for future Postgres-compatible managed
  database backends?
- What minimum Teams permissions are required for channel messages, DMs,
  Adaptive Cards, and channel creation?
- Should project Teams be created by Agentic Mesh, or should the first version
  bind to an existing Team created by an administrator?
- How should generated documentation commits be grouped: per work item, per
  slice, per role interval, or by explicit human approval?
- How should organization-level role template updates be rolled out safely to
  existing projects with local overrides?
- Which commercial features should be kept separate from the open source core
  without weakening the usefulness of the open source project?

## First Implementation Slice

The first Agentic Mesh slice should prove the enterprise spine without trying
to implement every role.

Scope:

- Docker Compose project with `router`, `control-plane`, configured role-agent
  containers, and `otel-collector`.
- permanent role template files for the starter SDLC roles.
- project override file declaring the `agentic-mesh-dev` project, configured
  role instances, and the project-specific SDLC flow overlay.
- file-backed message, state, artifact, and journal adapters.
- lifecycle policy with idle hibernation and wake-on-inbox behavior.
- worker adapter interface with `codex-cli` as the first concrete adapter.
- Teams connector design stub or minimal local connector if Teams credentials
  are not ready.
- visible flow through project-configured lifecycle states without hard-coding
  the handoff graph into role templates or worker adapters.

Acceptance criteria:

- `docker compose up` starts the router, OTEL collector, and role-agent
  containers.
- The control-plane can stop an idle role-agent instance after a grace period.
- The control-plane can restart a hibernated role-agent instance when a new
  inbox message arrives.
- Each agent instance has a distinct mounted volume and role-instance id.
- Role instances load permanent role template plus project override.
- A project can run at least two Engineering instances against the same role
  queue without duplicate claims.
- A project flow overlay declares SDLC lifecycle states, state owner roles,
  allowed work item types, and handoff targets.
- Work item messages carry `work_item_id`, `work_item_type`, and
  `lifecycle_state` so slices, features, and spikes can progress in parallel.
- A work item can be written to the role that owns the configured entry state.
- Each role claims work for the lifecycle state it owns, records configured
  artifacts, and emits the configured next-state handoff.
- The event journal records receive, claim, run, documentation update, handoff,
  and delivery events.
- OTEL traces show the same correlation id through routing and agent execution.
- OTEL traces record hibernate and wake events.
- All generated docs and decisions are inspectable as files.
