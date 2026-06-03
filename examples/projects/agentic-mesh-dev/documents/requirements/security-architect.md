# Security Architect Worklist

Status: sponsor-editable adoption output

## Role View

Security must be designed into the role mesh from the start: identity,
least-privilege tools, secret handling, connector permissions, data retention,
audit, and policy enforcement. The current Graph/Teams work also shows the
need for strong source attribution and echo suppression.

## Outstanding Work

- Define threat model for role containers, message queues, connector ingress,
  worker adapters, Git writes, secrets, and project workspaces.
- Define identity model for role instances, connector apps, human users,
  worker providers, and cloud resources.
- Add least-privilege permission guidance for Teams Graph channel read/send,
  Adaptive Cards, DMs, app registrations, and admin consent.
- Add secret reference validation and rotation procedures; never store token or
  secret values in Git or role config.
- Add source attribution checks for connector messages to distinguish human
  sponsor directives from Agentic Mesh bot output.
- Define write-path enforcement and tool allowlist behavior per role.
- Define data classification, redaction, and retention for prompts, raw Teams
  payloads, journals, docs, and evidence bundles.
- Add security release gate: dependency review, secret scan, config validation,
  and container baseline scan.
- Define commercial policy pack candidates: approval gates by risk, model
  allowlists, data residency, PII handling, retention, and audit export.

## Risks And Decisions

- Risk: App-only Graph permissions overreach. Mitigation: document minimum
  scopes, admin consent, channel restrictions, and audit logging.
- Risk: Worker adapters can write outside allowed project paths. Mitigation:
  enforce write paths in runtime/tool layer and test path escapes.
- Decision needed: first security baseline for OSS preview versus enterprise
  paid policy pack.

## Suggested Acceptance Criteria

- No secret values appear in config, examples, docs, committed state, or logs.
- Connector ingress can prove whether a message was human-authored or an
  Agentic Mesh echo before routing work.
- Each role has explicit tool and write boundaries.
