# AMV5-048 — Project-configured Azure DevOps adapter

## Outcome

Link a V5 work item to one Azure DevOps work item and read or update that link
only through the ADO organization, project, and credential declared by the
active project manifest. V5 lifecycle state remains authoritative.

## Scope and boundaries

- Add one project-scoped external-link record and one durable idempotent update
  operation. Do not add a second work-item lifecycle or a general integration
  workflow.
- Resolve the organization, project, and external credential reference from the
  active immutable manifest snapshot. Callers cannot supply or override them.
- Use only project-qualified ADO REST URLs. Every successful remote response
  must also contain an exact matching `System.TeamProject`; the URL alone is not
  treated as proof of scope.
- Store the linked ADO numeric ID and canonical project-qualified URL beside the
  runtime work item. Never store access tokens.
- Allow a small explicit set of ordinary work-item fields. Relations, identity,
  project, area, iteration, and arbitrary JSON Patch paths remain out of scope.
- Persist an update operation before the remote side effect. Exact retries first
  read the remote item: if the requested values are already present, complete
  locally without repeating the patch.
- Bound transient retries for transport failures, throttling, and service
  errors. ADO unavailability leaves the local operation pending and never
  changes the authoritative V5 work item.
- Distinguish authentication, permission, not-found, conflict, foreign-project,
  invalid-response, and transient-unavailable failures without logging tokens or
  response bodies.
- Reuse the existing generic external access-token and HTTP transport ports.
  The first concrete transport remains `httpx`.

## Acceptance criteria

- A link can be created only for an existing V5 work item under its active
  manifest's exact ADO binding; exact replay is idempotent.
- Linked records expose the ADO ID and canonical project-qualified URL while the
  V5 status/version remain unchanged.
- Reads and updates reject a response from any other ADO project, including when
  the numeric ID exists elsewhere in the organization.
- Concurrent or repeated update operation IDs cannot produce conflicting
  payloads or duplicate a field patch after an interrupted success.
- Transient failures receive no more than the configured attempts and retain a
  resumable pending operation with a safe diagnostic.
- Authentication, permission, conflict, not-found, invalid-response, and outage
  paths are covered by automated tests.
- A real qualification in the configured `seerstone/agentic-mesh` ADO project
  proves read, link, idempotent update, and foreign-project rejection without
  relying on global Azure CLI defaults.
