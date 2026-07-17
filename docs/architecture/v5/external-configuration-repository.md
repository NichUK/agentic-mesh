# External Configuration Repository

Agentic Mesh V5 organization configuration lives in the private Git repository
`https://github.com/NichUK/agentic-mesh-config`. The runtime reference is
stored in `config/v5/organization-config-repository.json`; package content is
not copied into the runtime package or worker images.

The repository provides versioned `system`, `role`, `flow`, `policy`,
`fragment`, `tool-profile`, and `project-override` packages. That external
repository's dependency-free validator checks a clean clone for package
identity, content containment, and common committed-secret patterns. V5 first
checks that the configured directory exposes the package schema and versioned
manifests, then resolves exact `kind/id@version` references and dependencies.

Resolution uses one fixed low-to-high precedence order: `system`, `policy`,
`fragment`, `role`, `flow`, `tool-profile`, and `project-override`. JSON objects
are recursively overlaid in that order; text content remains in ordered,
source-linked sections. The effective result includes a canonical SHA-256
digest and provenance containing each package reference, repository-relative
manifest/content path, and canonical UTF-8/LF source digest. Missing dependencies,
cycles, malformed manifests, invalid JSON and content-path escapes fail closed.

Deployments mount an approved clone at `/mesh/config`. Package manifests may
name `${secret:<name>}` references, but credential values, OAuth caches,
tokens, connection strings, and private keys remain in external secret stores
or host mounts. Immutable release activation and rollback remain separate
stories; package resolution never resolves secret values or changes an active
release.
