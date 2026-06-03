# Authentication Model

Status: draft design

Date: 2026-06-02

## Purpose

Agentic Mesh role agents and connectors must be able to use different
authentication methods without leaking credentials into system repos, project
repos, examples, journals, or generated artifacts.

Authentication is a deployment concern bound to a role instance, worker
adapter, connector, or tool adapter. It is not a role-template semantic and it
must not determine the lifecycle flow.

## Concepts

### Auth Method

An auth method is a system-defined capability in `config/auth-methods.yaml`.

It declares:

- method id
- category
- compatible adapters
- environment variables used by that provider
- whether a `secret_ref` is required
- whether a `mount_ref` is required
- description

### Auth Binding

An auth credential is a reusable project-level reference to one concrete
credential source. It selects an auth method and points at the deployment
secret or mount that will supply the credential material.

Example:

```yaml
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
  codex-shared-api-key:
    method: codex_api_key
    secret_ref: codex-shared-api-key
```

The same credential block can be reused by multiple roles or role instances.
This is the preferred shape for projects because it separates identity choices
from role definitions and allows several Codex OAuth accounts, access tokens,
or API keys to be active at the same time.

### Auth Binding

An auth binding is a worker or connector selection of an auth credential, or a
legacy inline auth method selection.

Example:

```yaml
worker:
  adapter: claude-code
  model: claude
  auth:
    credential: claude-security-oauth
```

The binding can contain:

- `credential`: reusable credential id from top-level `auth_credentials`.
- `method`: legacy inline method id from the auth catalog.
- `secret_ref`: logical secret name or provider path.
- `mount_ref`: logical mount name for credential cache or config directory.
- `env`: non-secret environment hints.
- `notes`: operator guidance.

The binding must never contain secret values.

## Validation Rules

- A role worker auth method must exist in `config/auth-methods.yaml`.
- The method must list the worker adapter in `applies_to`.
- Methods marked `requires_secret_ref` must provide `secret_ref`.
- Methods marked `requires_mount_ref` must provide `mount_ref`.
- Role bindings that use `credential` must reference a declared top-level
  `auth_credentials` entry.
- Project config may omit auth only while the worker adapter is a stub or while
  the deployment profile injects auth out of band.

## Resolution Flow

At runtime, the control-plane or worker adapter resolves auth in this order:

1. Load role instance project override.
2. Read worker or connector `auth` binding.
3. If it references a reusable credential, resolve that credential.
4. Validate the resolved binding against the system auth catalog.
5. Resolve `secret_ref` or `mount_ref` through the deployment profile.
6. Inject only the required environment variables, files, or identity settings
   into the role container.
7. Redact auth material from logs, traces, journals, and artifacts.

## Local Profile

The local profile can support:

- environment variable injection from a developer-owned `.env` file outside
  Git
- role-instance credential-cache mounts for trusted local experiments
- named Codex OAuth accounts, each logged into a separate `CODEX_HOME` under
  `state/worker_mounts/<mount_ref>`
- local secret files under `state/secrets/<secret_ref>` for access tokens and
  API keys
- manual-human worker with `manual_human_no_auth`

Credential cache mounts must be role-specific and must not mount a developer's
entire home directory or full global agent state.

The controller auth UI is the preferred local setup surface. It runs with the
control-plane, lists reusable credential status, stores secret-backed
credentials, and launches Codex OAuth device-auth sessions with
`CODEX_HOME=state/worker_mounts/<mount_ref>`. Collaboration connectors such as
Teams and Slack should link users into this controller-owned setup flow rather
than accepting tokens, API keys, or OAuth codes in chat messages.

## Enterprise Profile

Enterprise profiles should prefer:

- secret managers for API keys and access tokens
- managed identity or workload identity where provider support exists
- short-lived or rotated credentials
- per-project, per-role, or per-role-instance credentials where governance
  requires attribution

## Current Catalog

The initial auth catalog covers:

- Codex CLI: access token, OAuth cache, API key
- OpenAI API: API key
- Azure OpenAI: API key, Microsoft Entra ID
- Anthropic API: API key, bearer token
- Claude Code: OAuth token, OAuth cache, API key helper, Bedrock, Vertex,
  Foundry, Anthropic API key, Anthropic bearer token
- DeepSeek API: API key
- MiniMax API: API key
- Microsoft Graph connectors: delegated OAuth, app client secret, app
  certificate, federated identity
- Manual human worker: no auth

## Non-Goals

- Storing secret values in project YAML.
- Implementing real provider login flows in this slice.
- Automatically copying personal `auth.json` or `.credentials.json` files.
- Choosing a universal auth method for all agents.
