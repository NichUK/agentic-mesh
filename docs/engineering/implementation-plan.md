# Engineering Implementation Plan

Owner role: Engineering

This document captures durable implementation planning patterns for Agentic
Mesh. Slice-specific implementation plans must live in the relevant work-item
folder and link back here only when they establish a reusable pattern.

## V3 Direction

- Keep runtime services thin and adapter-oriented.
- Prefer interfaces and contract tests for broker, connector, document-library,
  worker, and deployment boundaries.
- Keep source, deployed runtime, runtime state, project config, and document
  library paths separate by default.

