# Project Memory

This file records the context needed to resume Agentic Mesh in a fresh chat
after opening `C:\Dev\agentic-mesh` as the workspace.

## Origin

This project grew out of work on `C:\Dev\dev-team-ai`, an OpenAgents-based AI
dev-team prototype.

The prototype demonstrated useful ideas:

- live role agents
- role-owned documentation
- channel handoffs
- approvals
- sponsor DMs
- role workflow readiness
- project/work item state
- testing role agents through live smoke tests

But OpenAgents became a poor architectural fit. We repeatedly had to patch
basic primitives such as identity, direct messages, Studio state, action
buttons, notifier behavior, and project boundaries. The conclusion was to stop
treating OpenAgents as the foundation and design a new runtime.

## Naming

The project is now **Agentic Mesh**.

Names considered:

- `DevTeam Mesh`: rejected because the product should expand beyond software
  delivery teams.
- `Enterprise Agent Network`: accurate but too generic.
- `AgentMesh`: concise but less distinctive.
- `TeamFabric`: less explicit about role-agent execution.

## Current Repository State

The repo was created at:

```text
C:\Dev\agentic-mesh
```

It now contains the first local runtime skeleton in addition to architecture
and configuration starter files.

Commits so far:

- `8b0924a Initial Agentic Mesh architecture`
- `1f11f72 Document hibernation and open-core direction`
- restart context commit: adds `AGENTS.md` and `MEMORY.md`

Uncommitted first development slice:

- `docs/implementation-slices/local-runtime-skeleton-v0.md`
- Python source-layout package under `src/agentic_mesh`
- config loader for organization defaults, role templates, and project overrides
- auth method catalog and per-role worker auth bindings
- human response type catalog and human response gate config shape
- reusable SDLC flow template under `config/flows/sdlc.yaml`
- project YAML schema under `config/schemas/project.schema.json`
- file-backed message store with pending, claimed, and completed queues
- append-only JSONL event journal
- artifact writer for Git-backed docs
- deterministic `StubCodexWorkerAdapter`
- runtime flow driven by a project SDLC overlay, not hard-coded worker behavior
- lifecycle store and control-plane tick for idle hibernate and wake-on-inbox
- file-backed connector outbox and typed human response request/received
  messages
- local Teams-style connector adapter for connector outbox processing and
  Adaptive Card rendering
- dogfood Teams identity model is one Teams bot per role, not one shared bot
  and not one bot per role instance
- role bot Entra app registrations, Azure Bot resources, Teams bot channels,
  Key Vault secret refs, Teams app catalog packages, and `dev-team`
  installations were provisioned in Azure resource group `agentic-mesh-dev`
- Teams package publish/install required a dedicated Graph installer app
  (`Agentic Mesh Teams App Installer`) because Azure CLI cannot request the
  delegated app catalog scope directly; publish was completed with device-code
  delegated Graph auth
- dogfood runtime should run its Docker network on the `linuxch` VM
  (`nich@10.0.0.65`) on `nichserv` (`10.0.0.4`), with external bot ingress
  assumed at `https://vpn.nixnet.com/api/messages`
- `docker-compose.linuxch.yml` adds a `teams-bot-listener` service on port
  `3978`; the listener accepts `/api/messages`, journals raw Teams activities,
  and normalizes `human_response.submit` invokes into role queue messages
- `docker-compose.linuxch.yml` switches `teams-connector` to
  `teams-bot-connector-loop`, using per-role Bot Framework credentials from
  ignored runtime files under `/home/nich/agentic-mesh/state/secrets`
- the Compose stack has been deployed to `/home/nich/agentic-mesh` on
  `linuxch`; `http://10.0.0.65:3978/healthz` works, but
  `https://vpn.nixnet.com/healthz` was not reachable from the Codex machine, so
  Azure Bot messaging endpoints were left on the placeholder URL
- SDLC/Teams smoke test `slice-teams-lifecycle-20260602210842` ran through the
  containerized lifecycle on `linuxch`: 13 agent runs, 12 Microsoft Teams
  channel posts, zero Teams Graph send failures, and final release status
  `completed_after_human_response`; evidence is in
  `docs/operations/sdlc-teams-smoke-test.md`
- caveat for that smoke test: outbound Teams messages used a delegated Graph
  token and appeared as the delegated user, not the `AM-*` role bot identities;
  the release approval response was injected into the local listener because
  the public `vpn.nixnet.com` route was not yet reachable
- SDLC/Teams smoke test `slice-bot-lifecycle-20260602212801` then ran through
  the containerized lifecycle using role bot outbound posting: 13 agent runs,
  12 `teams_bot_message_sent` events, zero bot send failures, visible Teams
  senders such as `AM-Business Analyst`, `AM-Engineering`, and
  `AM-Release Manager`, and final status `completed_after_human_response`
- remaining caveat for the bot-authored smoke: the approval was still injected
  into the local listener at `http://127.0.0.1:3978/api/messages`; public Azure
  Bot Service callbacks through `https://vpn.nixnet.com/api/messages` are still
  unproven
- SDLC/Teams smoke test `slice-public-card-lifecycle-20260602214816` then ran
  through the containerized lifecycle with AM-* role bot handoffs, an
  attachment-only Adaptive Card approval from `AM-Release Manager`, public HTTPS
  ingress through a temporary Cloudflare quick tunnel, 12
  `teams_bot_message_sent` events, zero bot send failures, one
  `human_response_received_from_teams`, and final status
  `completed_after_human_response`
- the temporary public endpoint used for that run was
  `https://tradition-matched-scientists-functions.trycloudflare.com/api/messages`;
  Azure Bot resources were temporarily updated to that endpoint, while the
  project config still records `https://vpn.nixnet.com/api/messages` as the
  intended dogfood endpoint
- remaining caveat for the public-card smoke: the approval was submitted as a
  Teams-style invoke payload over public HTTPS, not by an actual human click on
  the Adaptive Card routed through Azure Bot Service
- SDLC/Teams smoke test `slice-human-click-lifecycle-20260602215328` then
  verified the actual Teams UI click path: AM-* role bots posted 12 handoff/gate
  messages, `AM-Release Manager` posted an Adaptive Card in `approvals`, the
  user clicked Approve in Teams, the listener recorded
  `human_response_received_from_teams` with responder `Nicholas Overend`, and
  release review completed with status `completed_after_human_response`
- Teams approval card replacement has been implemented for the legacy
  `Action.Submit` message activity path by calling Bot Framework
  `updateActivity` against the original channel message; smoke test
  `slice-card-update-lifecycle-20260603091415` recorded
  `teams_bot_card_updated` for Teams message `1780474497910` and completed with
  status `completed_after_human_response`
- live OpenTelemetry export to SigNoz is now the target observability path:
  Agentic Mesh services send OTLP to the local `otel-collector`, which forwards
  to the external SigNoz gateway on `10.0.0.65:4317`
- organization naming defaults set `brand_prefix: AM`; role-agent telemetry
  service names render as `{brand_prefix}.{team_slug}.{role_id}.{ordinal}`, for
  example `AM.dev-team.engineering.1`, and Teams bot display names render as
  `AM-Engineering`
- Teams bot listener is still a dogfood ingress boundary and needs Bot
  Framework JWT validation before production exposure
- Docker Compose topology for router, control-plane, the configured
  `agentic-mesh-dev` role agents, and OTEL collector
- pytest coverage for config loading, duplicate-claim prevention, runtime
  handoff flow, and hibernation/wake behavior

## Current Design Summary

Agentic Mesh is an enterprise-oriented runtime for composing long-running AI
role agents into project-scoped collaboration networks.

Key decisions:

- One container per role-agent instance.
- Role templates are permanent or semi-permanent and rarely changed.
- Organization defaults provide cross-project settings such as global language,
  locale, documentation standards, conversation standards, handoff standards,
  and security defaults.
- Auth methods are catalogued centrally in `config/auth-methods.yaml`.
  Project roles bind to auth methods with `secret_ref` or `mount_ref`; secret
  values must not appear in project YAML.
- Human response templates are catalogued centrally in
  `config/response-types.yaml`.
- Human response gates are structured response contracts. Approval is one
  reusable response type, alongside yes/no, number, money, text, document, and
  URL response templates.
- Project document accountabilities define accountable owners, contributors,
  owner-review-on-contribution behaviour, and handoff gates.
- Project overrides specialize role templates for each project.
- A project may run multiple instances of the same role.
- Each instance has its own storage volume, inbox, outbox, journal, identity,
  tool boundary, and telemetry identity.
- The router routes messages and handoffs but does not coordinate work like an
  executive controller.
- The control-plane starts, stops, hibernates, wakes, health-checks, and
  reports status for role instances.
- Idle agents can hibernate after a grace period.
- Hibernated agents wake automatically when new work, DMs, mentions, scheduled
  work, or manual operator action requires them.
- Runtime queues are append-only and inspectable by default.
- Cloud-native backends such as Azure Service Bus, Azure Table Storage, Blob
  Storage, DynamoDB, SQS/SNS, and S3 are backend options behind ports.
- An append-only event journal is mandatory for audit and replay regardless of
  queue backend.
- Microsoft Teams is the first collaboration connector.
- Dogfood Teams integration uses one bot identity per role. Role instances
  remain internal runtime identities beneath the role bot.
- Slack should be supported later through the same connector abstraction.
- Worker/model providers are adapters: Codex, OpenAI, Anthropic, Claude Code,
  DeepSeek, MiniMax, and future providers should be replaceable.
- OpenTelemetry is required from the first implementation slice.
- Git owns config, docs, decisions, stories, test evidence, release records,
  and committed audit snapshots.

## BMAD And Roles

The role model is inspired by BMAD-style SDLC roles, but Agentic Mesh uses a
distributed architecture rather than a centralized workflow controller.
Agentic Mesh uses functional role titles rather than named agents.

Role provenance:

- The original `dev-team-ai` blueprint defined Product Manager, Solution
  Architect, optional Infrastructure Architect, Delivery Manager, QA Engineer,
  Release Manager, and AO Bridge.
- During prototyping, Enterprise Architect was added.
- Engineering was added so implementation could be handled by a role agent.
- Infrastructure Architect currently folds into Enterprise/Solution
  Architecture unless later separated.

Current role examples in this repo:

- `config/roles/business-analyst.yaml`
- `config/roles/product-manager.yaml`
- `config/roles/ux-designer.yaml`
- `config/roles/enterprise-architect.yaml`
- `config/roles/solution-architect.yaml`
- `config/roles/security-architect.yaml`
- `config/roles/platform-engineer.yaml`
- `config/roles/engineering.yaml`
- `config/roles/qa-engineer.yaml`
- `config/roles/technical-writer.yaml`
- `config/roles/delivery-manager.yaml`
- `config/roles/research-analyst.yaml`
- `config/roles/release-manager.yaml`

Current project config:

- `examples/projects/agentic-mesh-dev.yaml`

This project config is the current dogfood project for building Agentic Mesh.
It references the reusable `config/flows/sdlc.yaml` template. The runtime must
not hard-code Product to Engineering or any other fixed flow. Work items carry
`work_item_id`, `work_item_type`, and `lifecycle_state` so slices, features,
and spikes can progress independently and in parallel.

Repository boundary:

- This root repo is the Agentic Mesh system repository. It owns runtime code,
  default role templates, default configuration templates, stock/example
  flows, configuration schemas, docs, and tests.
- Real projects should live in separate project repositories or workspaces.
  They own project overlays, project docs, evidence, work items, channel
  mappings, and references to secrets by name.
- Example projects can live under `examples/projects/` only if they contain no
  credentials.
- Secrets, Codex auth files, Teams OAuth credentials, API keys, and live
  runtime state must not be committed to either system examples or project
  overlays.

These are starter examples, not final canonical templates. A future role
definition pass should create a proper starter role pack.

Potential starter pack:

- business-analyst
- product-manager
- ux-designer
- enterprise-architect
- solution-architect
- delivery-manager
- engineering
- qa-engineer
- technical-writer
- release-manager

Potential optional packs:

- research-analysis
- security
- cloud/infrastructure
- data
- support
- legal/compliance
- finance/procurement

## Future Product Backlog

Future feature:

**Stock Enterprise Flow Catalogs**

When a Research Analyst role is implemented, give it a research task to
identify common enterprise lifecycle patterns that could become optional stock
flow templates. Candidate areas include software delivery variants,
enterprise architecture review, security/compliance review, procurement and
vendor assessment, data governance, incident/support operations, and change
advisory/release governance.

Important constraint: stock flows must be optional starter templates. The core
runtime should continue treating flow as a per-project overlay, and projects
must be able to override lifecycle states, owners, handoffs, artifacts, and
parallel work item types.

## Open Source And Commercial Direction

The project should be open source with a commercial offering layered on top.

Open source should include the useful core:

- runtime
- role templates and project overrides
- auth method catalog and secret-reference based auth bindings
- local storage adapters
- event journal
- Docker Compose profile
- basic lifecycle control and hibernation
- OTEL
- worker adapter interface
- collaboration connector interface
- basic Teams connector where feasible

Commercial offering ideas:

- support
- advanced control-plane
- SSO/RBAC
- managed or assisted Azure/AWS deployment
- enterprise policy packs
- compliance and audit reporting
- cost dashboards
- managed storage/message backends
- onboarding and role-template customization

Principle: do not make the open source core hollow. Commercial value should be
enterprise operations, governance, support, and convenience.

## Implemented Slice

The first development slice is:

**Local Runtime Skeleton v0**

Scope:

- Python project skeleton.
- Docker Compose topology with router, control-plane, product-manager,
  engineering instance 1, engineering instance 2, and otel-collector.
- Config loader for organization defaults, role templates, project overrides,
  and project flow templates/overlays.
- File-backed message store.
- Append-only event journal.
- Basic claim semantics for multi-instance Engineering.
- Simple worker adapter stub before real Codex integration.
- Lifecycle state model with idle and hibernated states plus wake-on-inbox.

Verified acceptance criteria:

- project config loads and validates
- organization defaults load and validate
- referenced project SDLC flow template loads and validates state owners and
  handoff targets
- role instances derive from template plus project override
- two engineering instances compete for one role queue without duplicate claim
- work messages preserve `work_item_id`, `work_item_type`, and `lifecycle_state`
- parallel slices, features, and spikes preserve separate lifecycle context
- idle agent can hibernate after grace period
- hibernated agent wakes when an inbox message appears
- event journal records route, claim, hibernate, wake, and complete events
- OTEL spans or a local stand-in trace correlation exists

Current verification:

```powershell
pytest -q
$env:PYTHONPATH = "src"
python -m agentic_mesh.cli validate-config
```

Current messaging slice:

- `docs/implementation-slices/local-messaging-v0.md`
- role inbox queues remain the delivery path for role-agent work
- connector outbox queues live under local runtime state by logical channel
- runtime emits `human_response.requested` connector messages for
  `human_response` flow gates
- CLI can record `human_response.received` role messages for local testing

Current local Teams connector slice:

- `docs/implementation-slices/local-teams-connector-v0.md`
- `LocalTeamsConnectorAdapter` claims connector outbox messages by logical
  channel
- `human_response.requested` messages render into inspectable Adaptive Card JSON
- Docker Compose includes a `teams-connector` service
- Microsoft Graph channel posting works with a delegated token, but the linuxch
  dogfood connector now uses `BotFrameworkTeamsConnectorAdapter` so outbound
  SDLC handoff and approval messages appear as the configured `AM-*` role bots
- `BotFrameworkTeamsConnectorAdapter` sends `human_response.requested` as an
  attachment-only Teams Adaptive Card; sending text plus attachment through the
  proactive conversation-create path failed with
  `Activity resulted into multiple skype activities`
- Adaptive Card submit receive has a HTTP listener, and public HTTPS ingress
  was proven through a temporary Cloudflare quick tunnel; a real Teams UI card
  click through the bot endpoint was verified on
  `slice-human-click-lifecycle-20260602215328`; public callbacks through
  `vpn.nixnet.com` and Bot Framework JWT validation are still future hardening
  work
- generated Teams app packages live under ignored `build/teams-apps/`

Current container/config boundary:

- Agentic Mesh runtime containers should use a shared runtime image and must
  not bake organization or project configuration into the image.
- The dogfood Compose profile uses `agentic-mesh:local` for router,
  control-plane, role agents, Teams connector, and Teams bot listener.
- Build the local dogfood runtime image with
  `docker compose -f docker-compose.yml -f docker-compose.linuxch.yml --profile build-image build runtime-image`
  only when runtime code changes.
- Runtime inputs are mounted explicitly: `./config:/mesh/config:ro`,
  `./examples:/mesh/examples:ro`,
  `.:/mesh/workspaces/agentic-mesh`, and `./state:/mesh/state`.
- Runtime path selection is controlled by `AGENTIC_MESH_CONFIG_ROOT`,
  `AGENTIC_MESH_PROJECT_FILE`, `AGENTIC_MESH_WORKSPACE_ROOT`, and
  `AGENTIC_MESH_STATE_ROOT`.
- Project files declare a `workspace` block with `root`,
  `default_repository`, and one or more repository entries. The current
  dogfood project uses the mounted Agentic Mesh repo as
  `/mesh/workspaces/agentic-mesh`, and role `write_paths` plus flow
  `artifact_path` values are relative to that workspace.
- Rebuilding containers is only for runtime image changes. Configuration,
  project, documentation, state, and secret changes should become live through
  mounted external paths or service restarts, not image rebuilds.

Current open source and commercial plan:

- `docs/product/open-source-commercial-plan.md`
- recommended open source license is Apache-2.0 for the core, subject to legal
  review before public launch
- open source core should remain genuinely useful: runtime, role/project model,
  local storage and queues, event journal, Docker Compose, basic control-plane,
  OTEL, adapter interfaces, starter role packs, and basic collaboration
  connector support
- commercial boundary should focus on enterprise adoption friction: advanced
  control-plane UI, SSO/RBAC, multi-project operations, managed cloud backends,
  policy packs, audit/compliance reporting, cost dashboards, premium connector
  automation, support, onboarding, and role-template customization
- quick commercial wins are design-partner offers, enterprise readiness
  assessments, assisted Teams/Microsoft 365 setup, Azure deployment blueprints,
  support packages, and a read-only control-plane dashboard preview

## Recommended Next Slice

The next work should probably harden Local Runtime Skeleton v0 into a more
real local developer loop.

Candidate next slice:

**Local Runtime Loop v0.1**

Scope:

- Replace the router loop placeholder with a real routing service boundary.
- Add a long-running agent loop that respects lifecycle state before claiming
  work.
- Add first-class local OTEL spans using the collector already wired in
  Compose.
- Add role-instance volume paths matching the architecture document.
- Add a CLI smoke command that runs the configured project SDLC flow.
- Add schema validation or stricter config validation errors.
- Decide whether local lifecycle states should include `starting`,
  `hibernating`, and `failed` in v0.1 or wait for container orchestration.

## Important Non-Goals For Now

- Do not rebuild the full OpenAgents prototype.
- Do not start with a heavy database.
- Do not start with Azure Service Bus as the only queue backend.
- Do not build the configuration UI before config files are stable.
- Do not make Teams the only possible UI.
- Do not hard-wire Codex or any one model provider into the runtime semantics.
- Do not make the control-plane an executive workflow decision-maker.
