# AMV5-017 — Versioned Control API

## Outcome

Expose the durable V5 kernel through a versioned FastAPI boundary without
giving operators, the CLI, or later dashboard clients direct database access.

## Scope

- serve project, work, gate, approval, and queue operations under `/api/v1`;
- expose authorized agent, handoff, progress, configuration, and audit reads;
- advertise usage and recovery as unavailable until their ordered stories;
- authenticate external SHA-256 token records and authorize project/scope;
- use the authenticated subject as the durable mutation actor;
- return RFC 9457-style problems with a request identifier and publish OpenAPI.

Entra, dashboard reads, handoffs, usage, recovery, and config writes come later.

## Bootstrap authentication

`AGENTIC_MESH_V5_API_PRINCIPALS_FILE` must point to external hashes of random, high-entropy tokens; plaintext remains in the caller's secret store.

```json
{
  "principals": [
    {
      "subject": "bootstrap-operator",
      "token_sha256": "<64 lowercase hexadecimal characters>",
      "projects": ["*"],
      "scopes": ["project:create", "read", "write"]
    }
  ]
}
```

Set `AGENTIC_MESH_V5_DATABASE_URL`, migrate, then run `agentic-mesh-v5
api-serve`. Documentation is at `/docs`; OpenAPI is at `/openapi.json`.

## Acceptance criteria

- Implemented operations remain under `/api/v1` and retain store guarantees.
- Credentials fail closed without revealing bearer tokens or database details.
- A principal cannot read or mutate a project outside its explicit allow-list.
- Validation, conflict, authorization, and service errors use stable problems.
- OpenAPI describes version, bearer security, models, and planned domains.
- The server defaults to loopback and external database/authentication config.
- API, Postgres concurrency, boundary, and packaging tests pass.
