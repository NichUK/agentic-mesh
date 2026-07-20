# AMV5-038 — OneDrive DocumentStore adapter

## Outcome

Provide a provider-neutral synchronous `DocumentStore` contract and a
Microsoft Graph/OneDrive implementation for browsing and changing documents
inside one project-manifest root. Workers use the API and never require a
filesystem document mount.

## Scope and boundaries

- Resolve drive id, absolute remote root, adapter, and credential id from the
  active immutable project-manifest snapshot. Resolve the access token through
  an injected external credential provider on each operation; never persist,
  return, or log the token.
- Bind each store instance to exactly one named document root. Accept only
  canonical relative paths with no absolute form, backslashes, control bytes,
  `.` or `..` segments. Percent-encode Graph path segments after containment is
  established.
- Define backend-neutral metadata, page, content, create, and conditional
  update operations plus explicit not-found, conflict, permission, too-large,
  unavailable, and invalid-response errors. Future stores implement the same
  protocol without Graph types leaking into product semantics.
- List through Graph's children endpoint with selected metadata and bounded
  page size. Convert `@odata.nextLink` into an opaque local cursor containing
  only the validated relative path and skip token; never follow a caller-supplied
  URL.
- Read metadata before content to reject folders and oversized downloads.
  Follow Graph's one-time HTTPS download redirect without forwarding the bearer
  token to the pre-authenticated content host.
- Create and update through Graph upload sessions. Creates use conflict behavior
  `fail`; updates require an expected eTag and send `If-Match`. Upload sequential
  ranges without authorization headers to the pre-authenticated upload URL.
- Use 10-MiB chunks (a multiple of 320 KiB), accept `202` for intermediate
  ranges, and require a final `200` or `201` driveItem response. Bound total
  upload and download sizes through adapter configuration.
- Map Graph `401/403`, `404`, `409/412`, `413/507`, `429`, and `5xx` responses to
  stable product errors. Error messages include bounded status/code evidence,
  never response bodies, tokens, upload URLs, or request authorization.
- Use the existing mature `httpx` dependency behind a small transport protocol.
  Tests use a deterministic fake transport; no live tenant or credential is
  required for acceptance.
- Materialise production access tokens from one external, read-only credential
  root. Derive the mounted file from the manifest provider and credential
  reference, validate containment after resolving links, and read the file on
  every operation so an atomic host refresh takes effect without restarting the
  control plane. Never pass an access token through an environment variable.
- Configure the control API with one conventional document-root id and the
  external credential root. Both settings are required together; an incomplete
  production configuration fails at startup rather than silently disabling
  governance documents.
- Keep the refresh operation outside the container. The LinuxCH host obtains a
  fresh delegated Graph token from its existing Azure CLI login, atomically
  installs it at the project-scoped mounted path with the control-plane UID,
  and runs the same command on a systemd timer. Workers never receive this
  credential mount.

## Acceptance criteria

- A manifest-bound store can page folder contents, return stable metadata, read
  file bytes, create a file, and conditionally update it without any filesystem
  document mount.
- Root traversal, absolute/backslash paths, forged paging cursors, foreign
  project/root ids, and cross-root operations fail before a Graph request.
- Download redirects and upload-session requests never receive the Graph bearer
  token. Credential values are absent from Postgres, returned records, and
  raised errors.
- Existing-name create, stale-eTag update, missing items, permission denial,
  throttling/service outage, malformed Graph data, and configured size limits
  produce distinct explicit errors.
- An upload larger than 10 MiB uses ordered ranges, aligned non-final chunks,
  correct `Content-Range`, and one final metadata result.
- Two project document roots with different drives, paths, and credential
  references cannot address or authorize each other's content.
- The production API resolves governance documents from the active immutable
  manifest without test-only dependency injection, and refuses a missing root
  id, missing credential root, malformed reference, escaping link, oversized
  token, or token containing whitespace.
- Replacing a mounted token file changes the next Graph request without an API
  restart. Compose mounts the credential root only into `control`, and its
  environment contains paths and root ids but no bearer token.

## Live qualification binding

On 2026-07-20 the delegated `nich@quantauma.com` Graph session resolved the
`dev-agentic-mesh` Microsoft 365 group to its SharePoint `Documents` drive. The
concrete drive id is pinned in the V5 project manifest. A new, empty
`/Agentic Mesh V5` root and `work-items/amv5-live-001` folder were created for
V5; no retired V4 document was copied or treated as migrated V5 state.
