# Project Configuration Manual

Status: draft manual

Date: 2026-06-03

Schema: `config/schemas/project.schema.json`

Examples:

- `examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml`
- `examples/projects/example-project/agentic-mesh/project.yaml`

## Purpose

A project configuration file tells Agentic Mesh how to run a specific project.
It overlays stable organization defaults and role templates with project-level
choices:

- which project workspace and repositories agents can work in
- which roles participate and how many instances each role has
- which worker/model/auth binding each role uses
- which collaboration connectors, teams, channels, and bot identities to use
- which documents exist and which role is accountable for each one
- which flow or flow template governs work items
- which handoffs, consult routes, gates, and human responses apply

The schema validates the shape. This manual explains how to design the file.

## Minimal Shape

Every project file needs these required top-level fields:

```yaml
project_id: example-project
name: Example Project
workspace: ...
roles: ...
flow: ...
```

Optional top-level fields are:

```yaml
goal: ...
auth_credentials: ...
connectors: ...
document_library: ...
role_memory: ...
meshes: ...
document_accountabilities: ...
```

Use lowercase kebab-case for `project_id`, for example
`agentic-mesh-dev`, `customer-portal`, or `finance-ops`.

## Goal

`goal` is the project north star. Agentic Mesh passes it to every role-agent so
plans, handoffs, blocker reports, and artifacts stay tied to a focused project
goal rather than drifting into generic role activity.

```yaml
goal:
  description: Build Agentic Mesh into an open-core enterprise role-agent runtime
    with pluggable connectors for messaging, work intake, repositories, and
    delivery systems.
  success_measures:
    - Work is routed to the smallest appropriate set of roles.
    - Agents ask necessary clarifying questions before acting on ambiguity.
    - Agents acknowledge, plan, execute, and report blockers visibly.
  constraints:
    - Do not create documents just to record failure or status.
    - Preserve specialist decision ownership.
  guidance:
    - Every action should advance the current focus, reduce a meaningful risk,
      or clearly explain why progress is blocked.
```

Fields:

- `description`: durable project goal or mission the mesh is trying to achieve.
- `success_measures`: observable signs that work is staying on track.
- `constraints`: project-specific guardrails that role agents must respect.
- `guidance`: additional steering language for plans, handoffs, and blockers.

The goal is not a hidden controller decision. It is shared context for the
agents, similar to a sponsor brief or project charter. Roles may still disagree
or push back, but their reasoning should explain how the proposal advances the
goal or protects it from risk.

## Workspace

`workspace` declares the mounted project workspace and repositories agents can
use for real work.

```yaml
workspace:
  root: .
  default_repository: customer-portal
  repositories:
    customer-portal:
      type: git
      path: .
      default_branch: develop
      remote: git@github.com:example/customer-portal.git
```

Fields:

- `root`: project workspace root. Relative paths resolve under
  `AGENTIC_MESH_WORKSPACE_ROOT`. Absolute paths are allowed when the deployment
  mounts a fixed path.
- `default_repository`: repository id used for relative artifact paths and role
  `write_paths` unless a future work item overrides it.
- `repositories`: one or more repository entries.
- `repositories.<id>.type`: `git` or `filesystem`.
- `repositories.<id>.path`: repository root path relative to `workspace.root`,
  or an absolute mounted path.
- `repositories.<id>.default_branch`: optional expected branch.
- `repositories.<id>.remote`: optional repository remote URL.

Multi-repository example:

```yaml
workspace:
  root: .
  default_repository: front-office
  repositories:
    front-office:
      type: git
      path: repos/front-office
      default_branch: develop
    platform:
      type: git
      path: repos/platform
      default_branch: develop
    evidence:
      type: filesystem
      path: evidence
```

Runtime state and secrets do not belong in `workspace`. They are mounted and
configured separately by deployment.

## Document Library

`document_library` declares the durable project document root independently
from `workspace`. Relative roots resolve under the effective project workspace.

```yaml
document_library:
  backend: git
  root: ../../..
  structure_policy: togaf-sdlc-v1
  index_path: docs/00-index/document-library-manifest.json
  review_log_standard: same-document-review-log-v1
  versioning: backend
```

Fields:

- `backend`: `git`, `filesystem`, `onedrive`, or `sharepoint`. Git and
  filesystem are implemented first; Microsoft-backed libraries are adapter
  targets.
- `root`: document-library root path.
- `structure_policy`: document organization policy, initially
  `togaf-sdlc-v1`.
- `index_path`: generated document manifest path under the library root.
- `review_log_standard`: Markdown commenting convention. V1 uses visible
  same-document `## Review Log` entries.
- `versioning`: usually `backend`, meaning Git or OneDrive/SharePoint owns
  version history.

Use `python -m agentic_mesh.cli document-manifest --write` to generate the
current library manifest.

## Role Memory

`role_memory` configures the derived, source-linked cache each role can use to
avoid rereading the entire document library before every task.

```yaml
role_memory:
  enabled: true
  backend: filesystem
  root: memory/roles
  provenance_required: true
  refresh_from_document_library: true
  team_overlay_root: memory/team-overlays
```

Documents, ADRs, work-item artifacts, and the event journal remain canonical.
If role memory disagrees with the document library, the agent should refresh
memory from the canonical sources.

## Meshes

`meshes` declare peer team meshes and the roles that participate in each one.
Roles can be shared across meshes where that matches the organization.

```yaml
meshes:
  governance:
    name: Governance Mesh
    flow: governance
    roles:
      - enterprise-architect
      - delivery-manager
  sdlc:
    name: SDLC Mesh
    flow: sdlc
    parent_mesh: governance
    roles:
      - product-manager
      - engineering
      - qa-engineer
```

Cross-mesh handoffs create linked work items in the receiving mesh. The
receiving mesh applies its normal flow, reviews, and documentation rules.

## Roles

`roles` declares the role templates used by the project and how each role is
specialized.

```yaml
roles:
  engineering:
    template: engineering
    instances: 2
    worker:
      adapter: codex-cli
      model: codex
      reasoning_effort: medium
      sandbox_mode: workspace-write
      auth:
        method: codex_api_key
        secret_ref: openai-customer-portal-engineering-key
    instructions:
      - Preserve work_item_id and lifecycle_state in all handoffs and evidence.
      - Do not change deployment files without platform and release approval.
    write_paths:
      - src/**
      - tests/**
      - docs/engineering/**
    channels:
      primary: engineering
      handoff_inbox: engineering
```

Fields:

- `template`: role template id from `config/roles/`.
- `instances`: number of role-agent instances to run for this role.
- `worker`: model worker configuration for this project role.
- `instructions`: project-specific standing instructions added to the role.
- `write_paths`: project workspace paths the role may write to.
- `channels`: logical channel aliases used by connectors.

Role templates under `config/roles/` carry the reusable role charter. Project
roles should normally override local instructions, write paths, tools, and
channels rather than rewriting the charter.

Role template charters may include:

- `role_profile`: professional stance and operating model.
- `accountabilities`: durable responsibilities owned by the role.
- `decision_rights`: decisions the role owns, advises on, or escalates.
- `boundaries`: areas the role must not take over.
- `collaboration_style`: review, pushback, consult, and handoff behaviour.
- `quality_bar`: completion standards before the role marks work done.
- `memory_focus`: what belongs in source-linked role memory.
- `core_workflows`: repeatable role workflows with triggers, inputs, outputs,
  and artifacts.
- `standards_references`: standards or frameworks that informed the role.
- `anti_patterns`: common poor role behaviours to avoid.

See `docs/architecture/role-charters.md` and
`config/schemas/role-template.schema.json`.

`instances` supports parallel workers for the same role. For example,
Engineering can have two instances competing for the same Engineering queue
without cloning the role template.

`write_paths` are relative to the effective project workspace. Keep them as
narrow as possible. Use them to express role accountability, not hidden
security guarantees; tool adapters and runtime policy should enforce them later
as the platform matures.

## Worker And Auth

The `worker` block chooses how a role is executed.

Prefer reusable top-level credentials:

```yaml
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
  codex-shared-api-key:
    method: codex_api_key
    secret_ref: codex-shared-api-key
```

```yaml
worker:
  adapter: codex-cli
  model: codex
  reasoning_effort: medium
  sandbox_mode: workspace-write
  auth:
    credential: codex-product-oauth
```

Fields:

- `adapter`: worker adapter, such as `codex-cli`, `openai-api`,
  `anthropic-api`, `claude-code`, or `manual-human`.
- `model`: model or execution label used by the adapter.
- `reasoning_effort`: optional model reasoning effort hint. Supported values
  are `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`. When omitted,
  Agentic Mesh defaults to `medium`.
- `sandbox_mode`: optional worker command sandbox hint. Supported values are
  `read-only`, `workspace-write`, and `danger-full-access`. Worker adapters map
  this generic setting to their own execution controls. When omitted, Agentic
  Mesh defaults to `workspace-write`.
- `auth`: optional auth binding.

Auth binding fields:

- `credential`: reusable credential id from top-level `auth_credentials`.
- `method`: inline auth method id from `config/auth-methods.yaml`; keep this
  for compatibility or very small examples.
- `secret_ref`: logical secret name. This is a reference, not a secret value.
- `mount_ref`: logical mounted credential reference.
- `env`: non-secret adapter hints or env names.
- `notes`: optional human-readable detail.

Reusable credential fields:

- `method`: auth method id from `config/auth-methods.yaml`.
- `secret_ref`: logical secret name for API keys, access tokens, or bearer
  tokens.
- `mount_ref`: logical mounted credential reference for OAuth caches.
- `env`: non-secret adapter hints.
- `notes`: optional operator guidance.

Examples:

```yaml
auth:
  credential: codex-shared-api-key
```

```yaml
auth:
  method: codex_oauth_cache
  mount_ref: local-codex-ux-designer-home
```

```yaml
auth:
  method: manual_human_no_auth
```

Never place secret values, OAuth tokens, API keys, or credential files in a
project file.

Multiple Codex OAuth accounts can be active at the same time by giving each
account a different reusable credential and `mount_ref`. Multiple API keys or
access tokens work the same way with different `secret_ref` values. Roles can
share a credential when they should operate under the same account, or use
separate credentials when attribution, quota, or governance requires it.

## Connectors

`connectors` maps project collaboration surfaces into Agentic Mesh logical
channels. The current schema models Microsoft Teams.

```yaml
connectors:
  teams:
    adapter: teams-bot-connector
    identity_model: role_bots
    tenant_id: 00000000-0000-0000-0000-000000000000
    ingress:
      public_endpoint: https://example.com/api/messages
      listen_host: 0.0.0.0
      listen_port: 3978
      path: /api/messages
    team:
      id: 11111111-1111-1111-1111-111111111111
      name: dev-team
    channels:
      all-agents:
        id: 19:example@thread.tacv2
        name: all-agents
      engineering:
        id: 19:engineering@example
        name: engineering
    role_bots:
      engineering:
        display_name: AM-Engineering
        bot_id_ref: teams-bot-engineering-app-id
        secret_ref: teams-bot-engineering-secret
```

Connector fields:

- `adapter`: connector adapter id.
- `identity_model`: one of `shared_bot`, `role_bots`, or
  `role_instance_bots`.
- `tenant_id`: optional tenant id for Microsoft 365/Teams.
- `ingress`: optional public callback/listener settings.
- `team`: Teams team id and name.
- `channels`: logical channel ids and display names.
- `role_bots`: per-role bot identity references when `identity_model` is
  `role_bots`.

Ingress fields:

- `public_endpoint`: HTTPS endpoint used by the external service.
- `listen_host`: container listener host, often `0.0.0.0`.
- `listen_port`: container listener port.
- `path`: HTTP path, starting with `/`.

Role bot fields:

- `display_name`: bot display name shown in Teams.
- `bot_id_ref`: logical secret/config reference for the bot app id.
- `secret_ref`: logical secret reference for the bot credential.

Connector channel aliases should match role `channels` values. For example,
if a role says `primary: engineering`, the Teams connector should declare an
`engineering` channel.

## Document Accountabilities

`document_accountabilities` says which role is accountable for each project
document. Other roles may contribute, but the owner is accountable for
existence, completeness, and correctness.

```yaml
document_accountabilities:
  docs/product/stories.md:
    owner_role: product-manager
    accountability: accountable_owner
    can_edit_contributions: true
    review_on_contribution: true
    required_sections:
      - problem
      - target_user
      - scope
      - acceptance_criteria
    contributing_roles:
      - business-analyst
      - ux-designer
      - qa-engineer
    lifecycle_events:
      - document.contribution_added
      - document.owner_review_requested
      - document.owner_review_completed
```

Fields:

- `owner_role`: role accountable for the document.
- `accountability`: accountability label. The current default is
  `accountable_owner`.
- `can_edit_contributions`: owner may edit contributions from other roles.
- `review_on_contribution`: contributions should trigger owner review.
- `required_sections`: expected document sections.
- `contributing_roles`: roles expected or allowed to contribute.
- `lifecycle_events`: document-related events to emit or track.

Document paths are relative to the project workspace.

## Artifact Viewing

The control-plane status page links each artifact through
`/artifact-viewer/{artifact_path}` and opens it in a new browser tab. Raw source
remains available at `/artifacts/{artifact_path}`.

By default, the artifact viewer renders Markdown in-browser and enables Mermaid
diagrams. Deployments with a richer browser/document renderer, such as a
SeerSys D8Aroom-style browser plugin, should set:

```text
AGENTIC_MESH_ARTIFACT_RENDERER_URL_TEMPLATE=https://renderer.example/view?url={artifact_url}&path={artifact_path}
```

The template may use `{artifact_url}` for the absolute raw artifact URL and
`{artifact_path}` for the encoded project-relative artifact path. If the
template has no placeholders, Agentic Mesh appends both values as query
parameters.

## Flow

`flow` governs lifecycle states, handoffs, consult routes, gates, artifacts,
and supported work item types.

You can reference a stock flow template:

```yaml
flow:
  template: sdlc
```

You can also apply a partial overlay to a template:

```yaml
flow:
  template: sdlc
  overrides:
    work_item_types:
      - slice
      - feature
      - spike
      - defect
    states:
      implementation:
        purpose: Implement approved work and produce testable evidence.
```

Or define an inline flow:

```yaml
flow:
  flow_id: example-two-role-flow-v0
  name: Example Two Role Flow
  entry_state: product_definition
  work_item_types:
    - slice
    - feature
    - spike
  states: ...
```

Template ids resolve to `config/flows/<template>.yaml`.

## Sponsor-Initiated Work

Flows can define what happens when a sponsor starts by talking directly to any
agent.

```yaml
sponsor_initiated_work:
  allow_from_any_state: true
  default_work_item_type: spike
  default_intake_state: business_analysis
  capture_rule: When a sponsor asks any agent a question or asks for work that requires investigation, implementation, decision support, or cross-role input, the contacted agent must create or request a tracked work item before doing invisible side work.
  routing_rule: The contacted agent may start the work item in its own lifecycle state when it is clearly within that role's accountability, or route it to the default intake state when business framing, scope, priority, or sponsor intent is unclear.
  completion_rule: Sponsor-originated work must progress through required consult routes, forward handoffs, gates, evidence capture, and release or closure records before it is considered complete.
```

Fields:

- `allow_from_any_state`: whether sponsor-originated work can start from any
  lifecycle state.
- `default_work_item_type`: type used when the request is unclear or
  investigative.
- `default_intake_state`: fallback state for unclear business framing, scope,
  priority, or sponsor intent.
- `capture_rule`: standing instruction for turning direct requests into
  tracked work.
- `routing_rule`: how the contacted agent chooses a start state.
- `completion_rule`: what must happen before the work is complete.

## Flow States

Each flow state has an owner role, purpose, artifact path, gates, consult
routes, and handoffs.

```yaml
states:
  implementation:
    owner_role: engineering
    purpose: Implement the approved work item and collect implementation evidence.
    artifact_path: work-items/{work_item_id}/100-implementation-log.md
    gates: ...
    consults: ...
    handoffs: ...
```

Fields:

- `owner_role`: role accountable for this lifecycle state.
- `purpose`: what this state does.
- `artifact_path`: document or artifact file updated by work in this state.
  Lifecycle slice work should normally use `work-items/{work_item_id}/...`
  paths so every work item leaves an enterprise-grade dossier. Durable
  `docs/...` paths should be used for evergreen project knowledge, standards,
  ADRs, indexes, and operating guides.
- `gates`: required checks or human responses.
- `consults`: allowed bounded role-to-role requests for help.
- `handoffs`: lifecycle transitions when state work is complete.

The `owner_role` and all handoff/consult target roles must be configured in
`roles`.

## Handoffs

`handoffs` advance the work item to another lifecycle state after exit criteria
are met.

```yaml
handoffs:
  completed:
    target_state: quality_review
    target_role: qa-engineer
    message_type: sdlc.quality_review
```

Fields:

- `target_state`: next lifecycle state.
- `target_role`: owner role for the next state.
- `message_type`: optional message type. If omitted, the runtime defaults to
  `sdlc.<target_state>`.

The key, such as `completed`, is the status used to choose the handoff.

## Consult Routes

`consults` let the current owner ask another role for bounded input without
moving the work item out of the current lifecycle state.

```yaml
consults:
  product_scope:
    target_state: product_definition
    target_role: product-manager
    message_type: sdlc.consult.product_definition
    purpose: Clarify acceptance criteria, scope, priority, or user-visible behaviour.
```

Fields:

- `target_state`: lifecycle state context for the consult.
- `target_role`: role being consulted.
- `message_type`: optional message type. If omitted, the runtime defaults to
  `sdlc.consult.<target_state>`.
- `purpose`: why the current role may consult that target.

Consults may point backwards, forwards, or sideways in the lifecycle graph.
They do not replace required gates or forward handoffs.

## Gates

`gates` define checks that must be satisfied before a state can complete or
before a human/external response can be recorded.

Document owner review gate:

```yaml
gates:
  - gate_id: product_story_owner_review
    type: document_owner_review
    required_documents:
      - work-items/{work_item_id}/20-product-definition.md
    required_review_status: approved
    reviewer_role: product-manager
```

Human response gate:

```yaml
gates:
  - gate_id: release_decision_response
    type: human_response
    response_type: approve_not_approve
    prompt: Record the final release decision for this work item.
    requested_from: release-sponsor
    channel: approvals
    timeout: PT48H
    on_timeout: escalate
    completion_criteria:
      accepted_values:
        - approved
```

Common fields:

- `gate_id`: stable gate id.
- `type`: gate type, such as `document_owner_review` or `human_response`.

Document review fields:

- `required_documents`: document paths that must be reviewed.
- `required_review_status`: expected status, such as `approved`.
- `reviewer_role`: accountable reviewer role.

Human response fields:

- `response_type`: response template id from `config/response-types.yaml`.
- `prompt`: text shown to the human responder.
- `requested_from`: logical person, group, role, or authority.
- `channel`: logical connector channel.
- `timeout`: ISO-8601 duration, such as `PT48H`.
- `on_timeout`: timeout action, such as `escalate`.
- `completion_criteria`: type-specific criteria, such as accepted values.

Response templates currently include approval, yes/no, number, money,
single-line text, multiline text, document reference, URL, and document-or-URL.

## Complete Small Example

```yaml
project_id: example-project
name: Example Project
workspace:
  root: .
  default_repository: example-project
  repositories:
    example-project:
      type: git
      path: .
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
    instructions:
      - Keep product output concise and traceable to sponsor intent.
    write_paths:
      - docs/product/**
      - work-items/**
    channels:
      primary: product
      handoff_inbox: product
  engineering:
    template: engineering
    instances: 2
    worker:
      adapter: codex-cli
      model: codex
    instructions:
      - Claim work atomically and avoid duplicate implementation.
    write_paths:
      - src/**
      - tests/**
      - docs/engineering/**
      - work-items/**
    channels:
      primary: engineering
      handoff_inbox: engineering
flow:
  flow_id: example-two-role-sdlc-v0
  entry_state: product_definition
  work_item_types:
    - slice
    - feature
    - spike
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define product intent and acceptance criteria.
      artifact_path: work-items/{work_item_id}/20-product-definition.md
      handoffs:
        completed:
          target_state: implementation
          target_role: engineering
          message_type: sdlc.implementation
    implementation:
      owner_role: engineering
      purpose: Implement the configured example work item.
      artifact_path: work-items/{work_item_id}/100-implementation-log.md
      consults:
        product_scope:
          target_state: product_definition
          target_role: product-manager
          message_type: sdlc.consult.product_definition
          purpose: Clarify acceptance criteria or scope.
      handoffs: {}
```

## Validation

Run:

```powershell
python -m agentic_mesh.cli validate-config
```

For alternate files:

```powershell
python -m agentic_mesh.cli --project-file examples/projects/example-project/agentic-mesh/project.yaml validate-config
```

In containers, the relevant path variables are:

```text
AGENTIC_MESH_CONFIG_ROOT
AGENTIC_MESH_PROJECT_FILE
AGENTIC_MESH_WORKSPACE_ROOT
AGENTIC_MESH_STATE_ROOT
```

## Design Checklist

Before using a project file, confirm:

- no secret values are present
- `project_id` is stable and kebab-case
- `workspace` points at the mounted project repo or workspace
- every role `template` exists under `config/roles/`
- every flow owner, handoff target, and consult target is configured in
  `roles`
- every role channel alias maps to a connector channel when connectors are used
- `write_paths` are narrow enough for the role accountability
- document owners match the lifecycle state that depends on each document
- human response gates use response types from `config/response-types.yaml`
- sponsor-originated work rules are clear enough for direct agent contact
- parallel slices, features, spikes, defects, or research tasks preserve their
  own `work_item_id` and `correlation_id`
