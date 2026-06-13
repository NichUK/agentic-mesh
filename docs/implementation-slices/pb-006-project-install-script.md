# PB-006 Project Installation Script

Status: proposed

Owner role: platform-engineer

Supporting roles:

- solution-architect
- security-architect
- engineering
- qa-engineer
- release-manager
- technical-writer

## Goal

Create an idempotent project installer that reads project and organization
configuration, reconciles required Microsoft Teams and Entra resources, and
records exactly what changed or could not be changed.

The first implementation targets Microsoft Teams and Entra. The design must
keep provider operations behind adapter boundaries so later installers can
support Slack, GitHub Issues, Azure DevOps, other identity providers, and other
collaboration systems.

## Context

The current dogfood environment has v2 runtime code deployed, but tenant-side
Teams setup is not yet a repeatable product capability. Existing `AM-*` Entra
application registrations are still present, current project team/channel
bindings can drift from `project.yaml`, and local operator tokens may lack
Teams app catalog or personal app-installation scopes.

Manual setup is acceptable for investigation, but not for enterprise
installation or repeatable dogfood deployment.

## Inputs

- `project.yaml`, including:
  - project id and name
  - connector configuration
  - Teams tenant id, team binding, channel bindings, role identities, gateway
    identity, and secret references
  - sponsor/operator/release authority bindings where configured
- `organization.yaml` or equivalent external organization config, including:
  - naming conventions
  - connector defaults
  - credential policies
  - tenant/admin defaults
  - provider-specific install policy
- optional operator flags:
  - `--dry-run`
  - `--plan`
  - `--apply`
  - `--audit-report`
  - `--allow-create-team`
  - `--allow-create-channel`
  - `--allow-register-apps`
  - `--allow-install-apps`
  - `--allow-uninstall-stale`
  - `--allow-secret-rotation`

## Required Behaviour

The installer must:

- validate that project config and organization config are loaded from external
  mounts or operator-supplied paths, not baked image defaults
- validate Microsoft Graph, Teams, Bot Framework, and Entra scopes before any
  mutation
- locate or create the project Team when policy allows
- locate or create the default project channel when policy allows
- locate or create optional approval/status/focus channels when policy allows
- locate, create, or update gateway and role app registrations according to the
  configured identity model
- create or reference client secrets through approved secret providers without
  printing, logging, or committing secret values
- install or update configured Teams apps in the project Team where policy and
  permissions allow
- add configured agent identities or app members only where Teams requires it
  and the selected identity model supports it
- detect stale v1 `AM-*` app installs in configured Teams and personal scopes
  where permissions allow
- uninstall stale v1 app installs only when `--allow-uninstall-stale` is set
- report missing permissions explicitly, including the exact Graph scope family
  needed for each blocked operation
- emit an audit report with created, reused, updated, skipped,
  permission-blocked, failed, and deleted/uninstalled actions

## Idempotency Rules

Rerunning the installer with unchanged config must not create duplicates.

The installer must key resources by stable configured identity, not by display
name alone. It should compare:

- tenant id
- project id
- external team id or configured team name
- channel purpose and external channel id
- app registration app id or configured secret reference
- Teams app id/package id
- role id and connector identity binding

If a resource exists but differs from config, the installer should report a
planned update and require an explicit apply flag before mutating it.

## Security Requirements

- Secret values must never be written to repo-backed config, logs, audit
  reports, prompt context, Teams messages, or status pages.
- Setup-time permissions must be separated from normal runtime permissions.
- Broad app catalog, Teams app installation, directory write, and admin consent
  operations must be reported as operator actions.
- The script must fail closed when tenant id, team binding, authority mapping,
  credential provider, or required permissions are ambiguous.
- Any stale v1 removal must be reversible by reinstalling from the recorded
  app id/package id, or must state why it is not reversible.

## Acceptance Criteria

- Dry-run against a project with no tenant mutation produces a complete plan.
- Repeated dry-run and apply operations are idempotent.
- Missing `AppCatalog.*` or `TeamsAppInstallation.*` permissions are surfaced
  as actionable setup blockers, not generic Graph failures.
- Existing project Teams and channels are reused when they match config.
- Stale configured team ids are detected and reported with candidate matches.
- Stale v1 `AM-*` installs can be detected and uninstalled when permissions and
  flags allow.
- Credential references are generated or validated without exposing values.
- Audit report is written to the configured runtime/project audit location.
- Unit tests cover plan generation, idempotency, stale-resource detection,
  missing-permission classification, and secret redaction.
- Integration tests use fake Graph/Teams adapters; real-tenant smoke tests are
  documented separately for operators.

## Current Dogfood Findings

- Azure CLI is authenticated to tenant `564b667c-5b1a-4bbc-bb43-918b0b765a9b`
  as `nich@quantauma.com`.
- `project.yaml` still contains an older `dev-team` id that Graph could not
  fetch.
- Teams currently exposes a `dev-agentic-mesh` team with one `project` channel.
- No `AM-*` app install was found in that visible project team during manual
  inspection.
- Existing `AM-*` Entra application registrations are still present.
- Current token lacks app catalog and personal Teams app-installation scopes,
  so personal app uninstall and tenant app catalog inspection could not be
  completed manually.
- First installer implementation added `agentic-mesh-v2 install-project`.
  Dogfood dry-run and apply-mode audit reused the configured
  `dev-agentic-mesh` Team, reused the `project` channel, reused all existing
  role Entra app registrations, and planned installation for all configured
  role/gateway agents.
- Actual Teams app installation is still blocked because the installer needs a
  Teams app catalog package id per role/gateway identity and Graph scopes such
  as `AppCatalog.Read.All` plus `TeamsAppInstallation.ReadWriteForTeam`.
- Personal app install inspection remains blocked without
  `TeamsAppInstallation.ReadForUser` or equivalent self/user installation
  scopes.

## Review Log

- RL-001 | product-manager | sponsor-request | full document | Created the
  project installer slice from sponsor direction to replace manual Teams/Entra
  setup and stale v1 cleanup with an idempotent project installation command. |
  incorporated 2026-06-13
