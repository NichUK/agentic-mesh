# Governance

Agentic Mesh is currently governed by its maintainers.

The project is intended to remain an open source core with commercial enterprise
offerings layered around support, deployment, governance, compliance, and
operations. Governance decisions should protect the usefulness of the open
source project while allowing commercial work that funds continued development.

## Maintainer Responsibilities

Maintainers are responsible for:

- setting technical direction
- reviewing and merging pull requests
- triaging issues and discussions
- maintaining release quality
- protecting security reporting channels
- keeping the open source and commercial boundary clear
- recording significant architecture decisions

## Decision Process

Small implementation decisions can be made in issues and pull requests.

Use architecture decision records for durable decisions that affect:

- runtime topology
- role, project, or flow configuration semantics
- storage, queue, connector, worker, or deployment adapter boundaries
- security and identity model
- open source and commercial feature boundary
- compatibility guarantees

Architecture decisions belong in `docs/architecture/decisions.md`.

Product and commercialization decisions belong in `docs/product/` unless they
are also architecture decisions.

## Contribution Model

Agentic Mesh uses the Developer Certificate of Origin. See `CONTRIBUTING.md`.

Maintainers may decline contributions that:

- weaken the open source core
- hard-code one cloud, model provider, connector, or agent SDK into product
  semantics
- bypass role/project/flow configuration
- hide runtime state that should be inspectable
- introduce risky dependencies or incompatible licenses
- add enterprise-only assumptions to the core runtime
- lack tests or documentation for user-visible behavior

## Open Source And Commercial Boundary

The open source core should include the runtime, role/project model, local
storage, event journal, Docker Compose profile, basic control-plane behavior,
OpenTelemetry instrumentation, adapter interfaces, starter role packs, and
basic connector support.

Commercial offerings may include advanced control-plane UI, SSO/RBAC,
multi-project operations, managed cloud backends, policy packs, audit and
compliance reporting, cost dashboards, premium connector automation, onboarding,
role-template customization, and support.

The open source core should not be intentionally crippled. Commercial features
should solve enterprise adoption and operations problems.

## Releases

Until the first public release, versioning is experimental.

After public launch:

- use semantic versioning for published packages
- publish release notes
- document migrations and breaking changes
- keep security fixes clear and traceable
- prefer compatibility-preserving changes to project config schemas

## Maintainer Changes

Maintainers can be added by consensus of existing maintainers after sustained,
high-quality contribution and good judgment in design discussions.

Maintainers may be removed for inactivity, repeated violation of project
policies, or actions that put users or the project at risk.
