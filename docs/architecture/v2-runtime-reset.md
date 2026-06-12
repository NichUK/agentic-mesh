# V2 Runtime Reset

Status: draft implementation baseline

Date: 2026-06-11

## Intent

Agentic Mesh v2 resets the runtime spine after the dogfood v1 system proved the
core product need but exposed the wrong execution model.

V1 contains useful concepts: role templates, project overrides, flow templates,
safe-output tools, document library configuration, role memory, release targets,
and visible status surfaces. The failure was the composition. Safe-output calls
were converted back into an old result object, lifecycle logic still made hidden
decisions, queues auto-promoted unexpectedly, release management could describe
releases without reliably releasing, and project/runtime/source boundaries were
too easy to blur.

V2 treats the runtime as a new kernel rather than another patch layer.

## Repository And Deployment Boundaries

V2 separates three boundaries by default:

- **System source repo**: product source, tests, schemas, default templates,
  documentation, and deployment generators.
- **Deployed runtime**: installed image or artifact. It owns process execution
  and mounted operational config, but is not edited as a working checkout.
- **Project repos**: separate repositories containing project configuration,
  project documents, work-item dossiers, deployment overlays, and project source
  code.

Dogfood must use a separate project repo even when that project is Agentic Mesh
itself. The dogfood project repo may list the system source repo as a target
repository that agents can modify, but it must not be the deployed runtime tree.

Runtime state, secrets, connector cursors, worker temp files, and databases are
external mounts. The container image must not contain mutable project config or
credentials.

## Runtime Kernel

V2 starts with SQLite and database-backed runtime state. Repository interfaces
must not assume SQLite-specific behaviour so Postgres can become the enterprise
backend later.

The database is the operational source of truth for:

- queue items
- work items and state
- conversations
- agent runs and heartbeats
- safe-output calls
- approvals and human questions
- artifacts
- release evidence
- recovery state

An append-only event log table records audit and replay events, but dashboards
and services read from the runtime tables.

The work-item state machine is explicit and covers:

```text
queued -> shaping -> ready -> active -> release_review -> deploying -> released -> closed
```

It also handles:

```text
waiting_human
waiting_agent
waiting_external
blocked
recovering
canceled
superseded
failed_terminal
```

Any attention state must carry owner, reason class, next action, and
retryability.

## Agent Operating Model

V2 role agents are long-running role services. Each role service:

- keeps a stable role-instance identity
- claims work through the database
- records heartbeats
- loads role memory and recent context
- invokes the configured worker adapter
- records safe-output tool calls
- completes only through a terminal safe-output call

Provider-specific execution may still spawn a subprocess, but that is an
implementation detail behind the role-service boundary.

## Hibernation And Hydration

V2 should support long-running containerised role agents with idle hibernation,
but the product definition of "same state" must be logical state, not arbitrary
process memory.

The runtime should be able to stop or scale down idle role-service containers
after a configured idle period and later hydrate them back into the same logical
role-instance state by reloading:

- role instance identity
- active and pending assignments
- leases and heartbeat status
- role memory and team overlays
- conversation context and connector cursors
- work-item state and safe-output history
- prompt traces, journals, and recovery markers
- worker adapter configuration and credential references

The durable database, event log, document library, role memory, and connector
state are therefore the state of record. Containers are execution vessels.

The default hibernation policy should only hibernate at safe points: no active
tool call, no unflushed safe-output record, no uncommitted file operation, and
no worker subprocess whose result cannot be recovered. For active long-running
work, the role service should either continue running or enter a cooperative
suspend state after recording enough checkpoint data for retry or resume.

Full process checkpoint/restore, for example with CRIU, may be researched as an
advanced optimisation, but it should not be the open-source v2 baseline because
it is platform-sensitive and difficult to make reliable across worker providers.
The baseline should be deterministic logical hydration from durable runtime
state.

## Safe Outputs And Authority

Safe-output tools are the only durable mutation surface for agents.

Tools are role-scoped because specialists should not all have the same powers.
However, every role can request out-of-flow specialist help through governed
handoff and consult tools with an explicit reason.

Release Manager has release authority tools for deployment, no-deployment
disposition, sponsor approval, rollback plan, release decision, blocker
override, reopening flow, superseding work, and closure. A work item cannot
appear released unless release evidence has been recorded.

Status replies cannot claim durable work was created, promoted, deployed,
released, or closed. Those claims must come from the matching safe-output tool
and runtime event.

## Documentation Frameworks

V2 documentation is framework-driven. The first framework is `togaf-sdlc-v1`,
but the runtime treats it as configuration rather than hard-coded process.

Work-item documents are evidence dossiers. They should contain factual scope,
decisions, tests, risks, release evidence, and review logs. Status-only or
duplicated documents are invalid.

Role memory remains a source-linked cache. The document library, event log, and
work-item dossiers remain canonical.

## First MVP

The first v2 MVP is one real slice release:

1. Sponsor conversation or work-system item creates a queue item.
2. Product Manager shapes it and marks it ready.
3. Work item is promoted explicitly.
4. Engineering implements.
5. QA records evidence.
6. Release Manager records approval and deployment or no-deployment disposition.
7. Release Manager closes the work item with release evidence.

The current implementation baseline includes v2 topology validation, SQLite
schema, state-machine validation, role-scoped safe-output validation, role
service run enforcement, documentation framework validation, release evidence
closure rules, and a local end-to-end happy-path test.
