# AMV5-058 — Dashboard Entra authorization and D8Aroom proxy

## Scope

This slice makes the V5 control API the dashboard's authenticated host boundary and
exposes only the D8Aroom routes required by `@d8aroom/documents-react`.

The implementation deliberately has two independent checks:

1. Entra app roles grant an operation (`Viewer`, `Sponsor`, or `Operator`).
2. An external object-ID binding grants access to named Agentic Mesh projects.

Token claims cannot grant their own project access. The active, immutable project
manifest is the authority for the D8Aroom root IDs available inside that project.

## Acceptance criteria

- Tenant-specific Entra access tokens are validated for an RS256 signature, issuer,
  audience, lifetime, tenant ID, object ID, and an exact supported app role.
- `AgenticMesh.Viewer` grants read, `AgenticMesh.Sponsor` grants read/write, and
  `AgenticMesh.Operator` grants the existing organization operator scopes.
- Object-ID-to-project bindings are loaded from an external, secret-free JSON file.
- Existing hashed bootstrap tokens remain available for local bootstrap and tests.
- The D8Aroom proxy accepts only the component's roots, children, path, open,
  edit-open, content, content-stream, and comments routes.
- GET requires project read; content/comment mutations require project write.
- `/roots` is filtered to the active manifest's `d8a_root_id` values. Every node
  request requires one of those root IDs and fails before upstream access otherwise.
- Browser-supplied identity headers are ignored. The proxy creates a trusted D8Aroom
  principal from the already validated Mesh identity. The audience-bound Mesh access
  token is not forwarded; D8Aroom retains responsibility for its configured Graph
  app-only or OBO credential boundary.
- The upstream is a fixed HTTPS URL, redirects are rejected, safe response headers are
  allow-listed, JSON payloads are bounded, and no credential is stored in Git.

## External configuration

The API selects Entra authentication when any Entra setting is present and requires all
three together. A partial Entra deployment fails closed at startup; it never silently
falls back to a bootstrap token:

- `AGENTIC_MESH_V5_ENTRA_TENANT_ID`
- `AGENTIC_MESH_V5_ENTRA_AUDIENCE`
- `AGENTIC_MESH_V5_ENTRA_BINDINGS_FILE`

The bindings file contains only stable identity and authorization metadata:

```json
{
  "principals": [
    {
      "object_id": "00000000-0000-0000-0000-000000000000",
      "subject": "sponsor-1",
      "projects": ["agentic-mesh-v5"]
    }
  ]
}
```

D8Aroom is enabled with:

- `AGENTIC_MESH_V5_D8A_BASE_URL` (fixed HTTPS data-plane URL)
- `AGENTIC_MESH_V5_D8A_TENANT_SLUG`
- `AGENTIC_MESH_V5_D8A_TENANT_ID` (optional compatibility header)

Each exposed document binding adds a non-secret `d8a_root_id` to
`agentic-mesh/project.yaml`. Its existing credential remains an external reference.

## Test plan

- Generate a real RSA-signed token and prove issuer/audience/signature validation.
- Reject a foreign tenant, unbound object ID, unknown role, and tampered token.
- Prove Viewer/Sponsor/Operator permissions and project isolation.
- Mock D8Aroom, filter mixed roots, inspect the trusted principal and absent token headers,
  and reject a foreign root before the mock receives a request.
- Exercise API GET and mutation authorization and verify the OpenAPI operations.
- Run project-manifest, control-API, full V5, and clean-install test coverage.
