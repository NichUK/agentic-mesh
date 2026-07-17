# External Configuration Repository

Agentic Mesh V5 organization configuration lives in the private Git repository
`https://github.com/NichUK/agentic-mesh-config`. The runtime reference is
stored in `config/v5/organization-config-repository.json`; package content is
not copied into the runtime package or worker images.

The repository provides versioned `system`, `role`, `flow`, `policy`,
`fragment`, `tool-profile`, and `project-override` packages. That external
repository's dependency-free validator checks a clean clone for package
identity, content containment, and common committed-secret patterns. V5's
mount check in this slice verifies only that the configured directory exposes
the package schema and at least one versioned manifest; deterministic package
validation and composition belong to AMV5-005.

Deployments mount an approved clone at `/mesh/config`. Package manifests may
name `${secret:<name>}` references, but credential values, OAuth caches,
tokens, connection strings, and private keys remain in external secret stores
or host mounts. Later stories add deterministic resolution, immutable release
activation, and rollback; this slice establishes only the repository and
external mount boundary.
