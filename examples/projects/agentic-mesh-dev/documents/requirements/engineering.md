# Engineering Worklist

Status: sponsor-editable adoption output

## Role View

The codebase has a useful Python skeleton: config loading, file-backed message
stores, event journal, lifecycle tick, connector adapters, and tests. The main
engineering gap is turning the deterministic/stub runtime into a real role
worker system while keeping boundaries testable and provider-neutral.

## Outstanding Work

- Replace or augment the deterministic worker with real worker adapters:
  Codex CLI first, then OpenAI API and other providers behind the same port.
- Make stub worker mode explicit and incapable of marking artifact-heavy work
  complete unless configured for tests/demo.
- Harden Teams Graph ingress: real mention detection, connector echo
  suppression, bot identity filtering, cursor recovery, retries, and telemetry.
- Implement config reload signalling for long-running listener containers so
  token/channel/config changes do not require container recreation.
- Add a project builder command that reads project YAML and writes Compose
  under the project folder only.
- Add dead-letter handling and retry metadata for message store and connector
  outbox.
- Add file locking or cross-platform atomicity tests for concurrent role
  instances.
- Add integration tests for multi-instance Engineering claim competition.
- Add a real control-plane loop with health checks, wake decisions, and
  container lifecycle adapter boundaries.
- Replace journaled trace stand-ins with OpenTelemetry SDK spans and metrics.

## Technical Debt

- Teams Bot ingress currently reaches into private methods in places; extract
  stable intake services.
- Local VM deployment should be scripted from Git commits rather than manual
  file copies.
- Connector auth/token handling needs rotation and permission diagnostics.
- Runtime state folders need cleanup/compaction policies while preserving
  append-only audit history.

## Risks And Decisions

- Risk: Provider-specific worker features leak into role semantics. Mitigation:
  keep normalized request/result contracts and tool protocol.
- Risk: File-backed queues are too weak for concurrent local use. Mitigation:
  define limits, add locks, and keep cloud queues behind the same port.
- Decision needed: whether `router`, `control-plane`, and `agent-runtime`
  remain separate services in v0 or share a process for local simplicity.

## Suggested Acceptance Criteria

- `pytest -q` covers Graph ingress echo loops, all-agents routing, message
  claims, lifecycle wake, and project build boundaries.
- One real worker adapter can complete a docs-only work item and produce
  useful artifacts.
- A local Compose run can be rebuilt from committed project config.
