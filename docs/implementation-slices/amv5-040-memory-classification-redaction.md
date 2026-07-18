# AMV5-040 — Memory classification and redaction

## Outcome

Govern every shared-memory create and update before persistence. Secrets are rejected, supported
personal data is redacted without retaining originals, and organization memory requires positive
generic evidence. Missing or uncertain evidence remains project-local.

## Boundary

- Use one deterministic, local policy at the `SharedMemoryStore` boundary; workers cannot select an
  ungoverned organization scope or bypass the policy through an update.
- Treat the current project id plus configured aliases as project identifiers. Match complete tokens
  case-insensitively across subject, summary, tags, and source reference.
- Hard-reject private keys, bearer credentials, secret/token assignments, credential-bearing
  URLs, and signed access query strings. Do not persist rejected text or secret evidence values.
- Redact email addresses and international-style telephone numbers only in the summary. Persist the
  redaction type and count, never the original value. Sensitive structured fields reject instead.
- Classify as organization-generic only when content has no project identifier and cites a policy or
  release source beneath the current organization namespace. Other clean content is uncertain.
- Preserve requested local scope. An organization-role request is promoted only for an
  generic result; project-specific or uncertain results fail closed to project-role scope.
- Store classification, reason codes, and redaction counts in current and immutable
  revisions. Re-evaluate updates so organization memory cannot gain project-specific content.
- Keep semantic classifiers and enterprise DLP providers behind future policy
  adapters. This slice implements the small deterministic baseline needed for safe operation.

## Acceptance criteria

- Uncertain and project-specific organization requests persist only as project-role memory.
- Secret fixtures are rejected before any entry, revision, or evidence row is written.
- Supported personal data uses typed placeholders and originals do not appear in Postgres.
- A clean organization policy/release source creates organization memory with durable evidence.
- Project ids and configured aliases in any governed field prevent organization promotion.
- Updates cannot introduce secrets, project identifiers, or uncertainty into organization memory.
