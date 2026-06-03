# SigNoz OpenTelemetry

Status: dogfood runtime target

Date: 2026-06-03

Agentic Mesh sends live OpenTelemetry logs, traces, and metrics to the SigNoz
stack running on `linuxch`.

## Runtime Route

Agentic Mesh containers export OTLP to the local project collector:

```text
agentic-mesh service -> agentic-mesh-otel-collector -> 10.0.0.65:4317 -> SigNoz gateway -> SigNoz
```

The Agentic Mesh collector receives OTLP on `4317` and `4318` inside the
Compose network. On `linuxch`, it is also attached to the external
`observability` Docker network for local observability access, but the primary
export target is the external SigNoz gateway on `linuxch`:

```text
10.0.0.65:4317
```

The observability gateway must forward to the SigNoz collector. If operating
inside the monitoring Compose network, use the internal service name:

```text
signoz-otel-collector:4317
```

If the SigNoz collector is started with OpAMP-managed pipelines, confirm that
the effective collector config has not been replaced with `nop` pipelines. In
the current dogfood deployment the SigNoz collector is run from the mounted
static config so OTLP receivers on `4317` and `4318` remain active.

Do not point Agentic Mesh role containers directly at SigNoz. The local
collector is the project boundary for batching, retries, and future sampling or
redaction policy.

## Service Names

Service names come from organization naming defaults.

Current dogfood format:

```text
{brand_prefix}.{team_slug}.{role_id}.{ordinal}
```

Example:

```text
AM.dev-team.engineering.1
```

This differentiates similarly named roles across teams, such as
`AM.dev-team.engineering.1` and `AM.accounting.engineering.1`.

Shared runtime services use the same naming policy with their component name:

```text
AM.dev-team.control-plane.1
AM.dev-team.teams-connector.1
AM.dev-team.teams-bot-listener.1
```

## Trace A Slice

Search SigNoz traces by any of:

- `work_item_id`
- `correlation_id`
- `service.name`

A full SDLC slice trace should include spans similar to:

- `work.enqueue`
- `queue.wait`
- `work.claim`
- `agent.run`
- `worker.run`
- `artifact.write`
- `handoff.emit`
- `connector.outbox.enqueue`
- `connector.process`
- `teams.send`
- `human_response.request`
- `human_response.wait`
- `teams.receive`
- `teams.card.update`
- `human_response.record`
- `work.complete`

For approval decisions, search logs or traces by `gate_id` and
`response_value`. The release gate currently records
`gate_id=release_decision_response` and `response_value=approved` when the
release sponsor approves.

## Live-Only Scope

This slice emits live telemetry only. Historical journal replay from JSONL into
SigNoz is intentionally left for a later tool.
