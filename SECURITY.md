# Security Policy

Agentic Mesh is early-stage software for running long-lived role agents,
connectors, queues, and project automation. Treat deployments as experimental
unless you have independently reviewed and hardened the configuration.

## Supported Versions

Until the first public release, only the latest `main` branch is supported for
security fixes.

After tagged releases begin, this file should be updated with the supported
release window.

## Reporting A Vulnerability

Please do not report vulnerabilities through public GitHub issues.

Preferred route:

- Use GitHub private vulnerability reporting for this repository, if enabled.

Fallback route:

- Contact the maintainers privately through the security contact published in
  the repository profile or project website.

If no private route is available yet, open a public issue that says only that
you need a private security contact. Do not include vulnerability details,
proof-of-concept code, tokens, logs, tenant ids, or customer information in the
public issue.

## What To Include

Please include:

- affected version or commit
- deployment profile, such as local Docker Compose or cloud deployment
- affected component, such as runtime, connector, control-plane, auth, storage,
  or worker adapter
- reproduction steps
- expected impact
- whether any secrets, customer data, or external services were exposed

## Scope

Security-sensitive areas include:

- secret handling and auth bindings
- worker adapter credential injection
- collaboration connector ingress and outbound posting
- Microsoft Teams bot callbacks and Adaptive Card actions
- queue claiming, handoff routing, and event journal integrity
- file-backed storage path handling
- project workspace write boundaries
- generated artifacts and audit evidence
- OpenTelemetry redaction
- Docker Compose and cloud deployment profiles

## Current Hardening Notes

- Do not expose the Teams bot ingress listener to the public internet without
  Bot Framework JWT validation and TLS termination.
- Do not commit generated runtime `state/`, secret files, OAuth tokens,
  tenant-specific app ids, or customer workspaces.
- Use least-privilege app registrations, service principals, managed identities,
  and connector permissions.
- Keep provider SDK credentials out of role, project, and organization YAML.
  Config files should reference secret names or mount refs only.

## Disclosure

Maintainers will acknowledge valid private reports as quickly as possible and
coordinate a fix, release, and public advisory when appropriate.
