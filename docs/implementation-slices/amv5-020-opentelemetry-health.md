# AMV5-020 — OpenTelemetry and Health Reporting

## Outcome

Make V5 control-plane activity traceable through one standards-based telemetry
boundary and expose separate, redacted liveness and readiness reports.

## Scope

- add a small OpenTelemetry service wrapper for request, operation, and health
  spans plus low-cardinality request and health metrics;
- preserve incoming W3C trace context and return the active trace context so an
  operator can join a sponsor/API request to later work;
- attach safe project, work-item, queue, correlation, and request identifiers to
  spans without recording authorization headers, request bodies, prompts,
  database URLs, lease tokens, or connector credentials;
- provide a reusable operation-span contract for queues and the later turn,
  document, handoff, configuration-release, and recovery implementations;
- configure OTLP export only from standard external environment settings; and
- expose unauthenticated liveness and readiness probes while retaining
  `/api/v1/health` as the CLI-compatible readiness report.

Collector deployment, dashboards, alert policy, and domain implementations that
do not yet exist remain in their ordered later stories.

## Acceptance criteria

- An incoming valid `traceparent` is the parent of the API request span, and the
  response exposes the active trace context and stable request ID.
- Request spans use route templates, record safe domain identifiers and outcome,
  and do not contain tokens, bodies, database URLs, or raw unmatched paths.
- Request count/duration and dependency health metrics use bounded attributes
  rather than project or work-item identifiers.
- `/api/v1/health/live` does not depend on Postgres; readiness reports the
  packaged and applied schema state and returns HTTP 503 when Postgres is
  unavailable or migrations are pending.
- Health failure details use stable reason codes and never expose connection
  strings, credentials, exception messages, or stack traces.
- OTLP export is disabled unless externally configured, and runtime startup does
  not require a collector.
- Automated trace continuity, safe-attribute, metric, degraded-health, OpenAPI,
  V5-boundary, packaging, and regression checks pass.
