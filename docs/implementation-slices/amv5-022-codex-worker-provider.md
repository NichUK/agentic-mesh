# AMV5-022 — Codex Worker-Provider Adapter

## Outcome

Give V5 one provider-neutral execution boundary and one local Codex app-server
implementation without importing V4 runtime code or exposing Codex wire models
to product workflow logic.

## Scope

- define small synchronous protocols for a provider, warm engine, conversation
  thread, and active turn;
- define provider-neutral thread/turn requests, sandbox and approval policies,
  lifecycle events, completion states, and safe error categories;
- implement the Codex adapter with the official `openai-codex` Python SDK,
  pinned to the same Codex version as the V5 worker image;
- use the SDK's local stdio app-server transport and stable API surface;
- map Codex notifications, completion states, interruption, and exceptions at
  the adapter edge so downstream code never consumes Codex JSON-RPC methods,
  generated models, or exception text;
- preserve external `CODEX_HOME` and other explicitly supplied process
  environment settings without copying credentials into configuration, logs,
  errors, or object representations; and
- close the local app-server deterministically and make repeated close calls
  harmless.

Warm-engine lifetime policy, durable thread affinity, OAuth-cache mounting, and
prompt-version pinning are implemented by AMV5-023 through AMV5-026. This slice
only provides the boundary those stories need.

## Acceptance criteria

- A fake provider implements the V5 protocols without importing Codex and can
  drive the same neutral lifecycle consumed by callers.
- The Codex implementation starts and initializes one local stdio app-server,
  starts or resumes a thread, starts a turn, streams neutral events, interrupts
  the exact active turn, and closes cleanly.
- Codex notification names and generated SDK models do not cross the adapter
  boundary; unknown notifications are ignored safely.
- Completed, interrupted, and failed turns map to distinct neutral completion
  states. Overload/transport failures are retryable; invalid requests are not;
  safe errors contain no raw provider message, configuration, or credential.
- Thread and turn options map explicitly to supported SDK sandbox and approval
  presets; invalid local request values fail before the provider is called.
- A real-provider smoke starts the installed SDK runtime, creates an ephemeral
  thread without invoking a billed model turn, verifies runtime metadata, and
  closes without leaving an app-server process.
- Unit, real-provider, V5 boundary, dependency, worker-image, packaging, and
  regression checks pass, and no V5 source imports `agentic_mesh_v4`.
