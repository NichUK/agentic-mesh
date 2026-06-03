# Codex Authentication For Agent Containers

Status: draft design

Date: 2026-06-02

Source checked: Codex manual fetched 2026-06-02.

## Problem

Agentic Mesh role agents may run in separate containers while using Codex as a
worker adapter. Those containers need a way to authenticate Codex without
committing credentials into the system repo or project repos.

## Codex Auth Options

The Codex CLI supports:

- ChatGPT sign-in for subscription and workspace-governed access.
- API key sign-in for usage-based automation.
- Codex access tokens for trusted non-interactive local workflows in ChatGPT
  Business and Enterprise workspaces.

Codex local state defaults to `CODEX_HOME`, normally `~/.codex`. File-based
credential storage uses `auth.json` under `CODEX_HOME`. That file contains
access tokens and must be treated like a password.

## Recommended Agentic Mesh Pattern

Agentic Mesh should model worker authentication as a secret reference, not as
project configuration content.

Project config can say:

```yaml
worker:
  adapter: codex-cli
  model: codex
  auth:
    secret_ref: codex-agentic-mesh-dev-token
```

The local deployment profile resolves `secret_ref` at container start and then
chooses one of these trusted patterns:

- Inject `CODEX_ACCESS_TOKEN` as an environment variable for ephemeral
  non-interactive runs.
- Pipe `CODEX_ACCESS_TOKEN` to `codex login --with-access-token` inside the
  container when a persistent container-local Codex login is required.
- Mount a role-instance-specific `CODEX_HOME` volume that already contains a
  file-based Codex login cache, only for trusted local developer scenarios.

The preferred enterprise automation path is `CODEX_ACCESS_TOKEN` backed by a
secret manager. The preferred local prototype path can be a developer-managed
secret file or environment variable outside Git.

The broader provider-neutral auth catalog lives in `config/auth-methods.yaml`.
The architecture for all worker and connector auth bindings is documented in
`docs/architecture/authentication.md`.

## What Not To Do

- Do not commit `auth.json`.
- Do not copy a developer's full `~/.codex` directory into images.
- Do not bake Codex credentials into Docker images.
- Do not put access tokens, API keys, Teams secrets, or OAuth refresh tokens in
  project YAML.
- Do not reuse one human's personal token as the long-term identity for
  unrelated projects or teams.

## Identity Model

Eventually each role instance should have a distinct runtime identity, but
Codex's current auth primitives are user/workspace or token based. Agentic Mesh
should start with explicit workflow-owner tokens and record the role instance
identity in its own event journal.

Possible future identity strategies:

- one Codex access token per environment or project
- one Codex access token per role
- one Codex access token per role instance where governance requires it
- API key authentication for provider-agnostic or CI-style workers
- manual-human worker adapter where no model credential is available

The credential strategy should be a deployment-policy choice, not a runtime
semantic baked into roles.

## Local Compose Direction

The local Compose profile should eventually mount:

```text
project workspace -> /mesh/workspaces/<project-or-repo>
role volume       -> /var/lib/agentic-mesh/roles/<instance-id>
CODEX_HOME        -> /var/lib/agentic-mesh/roles/<instance-id>/codex-home
```

For token-based startup, the container entrypoint can run:

```shell
printf '%s' "$CODEX_ACCESS_TOKEN" | codex login --with-access-token
```

or skip persistent login and allow Codex to read `CODEX_ACCESS_TOKEN` directly
for non-interactive execution where supported.

## Open Questions

- Should the open source local profile support mounting a developer-provided
  Codex `auth.json`, or only environment-token injection?
- Should role instances share one project-level Codex credential by default, or
  require role-level credential references from the start?
- How should token refresh be persisted for hibernated containers?
- How should Agentic Mesh surface expired or revoked Codex credentials to the
  control-plane and project journal?
