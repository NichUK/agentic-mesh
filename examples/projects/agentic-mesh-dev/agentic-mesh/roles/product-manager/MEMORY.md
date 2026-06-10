# Product Manager Memory

## Operating Notes

- Source: `examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml`.
- Documents, ADRs, work-item artifacts, event journal entries, and threaded
  context are canonical. This file is a concise cache for recurring context.
- Keep entries source-linked to a document, work item, queue item, event, or
  sponsor conversation. Do not store secrets or raw connector identifiers.

## Current Project Context

- Agentic Mesh is an open-core, enterprise-oriented role-agent runtime.
- Messaging inputs are conversational by default unless they come from a
  dedicated work-item system or an agent proposes tracked work through
  safe-output tools.
- Source: Teams DM on 2026-06-10 for queue
  `queue-3a117c220270466ba30378dff4e722d4` and work item
  `work-96e5f41774ea44bc8df225a9783b7516`. Sponsor clarified that the
  `/status` dashboard slice is not only a timestamp/header alignment defect;
  compact data display and readable dense rows are in scope.

