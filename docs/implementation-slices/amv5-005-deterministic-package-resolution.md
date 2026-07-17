# AMV5-005 — Deterministic Package Resolution

## Outcome

Resolve exact, versioned configuration-package references from the external
organization repository into one inspectable effective configuration.

## Scope

- load selected packages and their exact dependencies;
- reject missing packages, invalid references, malformed content and cycles;
- apply one documented precedence order, with project overrides last;
- render JSON content through a small recursive merge and retain ordered text
  sections;
- calculate a canonical SHA-256 digest and record source-file provenance; and
- expose the result through the V5 CLI.

Version selection, release activation, secret resolution and runtime prompt
pinning belong to later stories. This slice does not add a package manager,
templating language or dynamic plugin system.

## Acceptance criteria

- Resolution produces the same output and digest for the same package content,
  independent of clone location or caller reference order.
- Dependencies are included exactly once and missing or cyclic dependencies are
  rejected with a useful error.
- Precedence is `system`, `policy`, `fragment`, `role`, `flow`, `tool-profile`,
  then `project-override`.
- Project override JSON values replace lower-precedence values while unrelated
  nested values are retained.
- Every selected manifest and content file has its package reference,
  repository-relative path and SHA-256 digest of its canonical UTF-8/LF
  content in provenance.
- Automated tests cover precedence, cycles, missing dependencies, containment,
  deterministic ordering and a golden rendered result.
