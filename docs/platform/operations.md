# Platform Operations

Owner role: Platform Engineer

This document records durable operational guidance for Agentic Mesh runtime
platforms. Work-item deployment evidence belongs in work-item dossiers and
release records.

## Current Operating Model

- V3 role agents run as long-lived role services.
- Runtime state, project config, organisation config, document library, and
  source repositories are mounted separately.
- Observability should use OpenTelemetry logs, traces, and metrics.

