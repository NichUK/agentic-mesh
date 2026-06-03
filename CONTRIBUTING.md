# Contributing To Agentic Mesh

Thanks for helping improve Agentic Mesh. This project is intended to have a
useful open source core with commercial enterprise offerings layered around
deployment, governance, support, and operations.

## Start Here

- Read `README.md` for orientation.
- Read `docs/architecture/agentic-mesh-design.md` before proposing runtime,
  role, connector, storage, or control-plane changes.
- Read `docs/product/open-source-commercial-plan.md` before proposing a feature
  that may affect the open source and commercial boundary.
- Open an issue before large design changes, new runtime adapters, new
  connector families, or behavior that changes project configuration shape.

## Development Setup

From a clean checkout:

```powershell
pip install -e .[dev]
pytest -q
python -m agentic_mesh.cli validate-config
```

The project is local-first. Prefer small, testable slices, file-backed adapters,
clear configuration, and inspectable runtime state.

## Pull Request Expectations

- Keep pull requests focused.
- Include tests for behavior changes.
- Update documentation when configuration, runtime behavior, architecture, or
  user workflows change.
- Do not commit secrets, tenant ids, tokens, generated runtime state, customer
  data, or machine-local paths.
- Keep provider-specific behavior behind adapters.
- Preserve project-level flow configuration; do not hard-code workflow decisions
  into role templates, the router, or the control-plane.

## Architecture Principles

Contributions should preserve these boundaries:

- One container per role-agent instance.
- Stable role templates with project-specific overrides.
- Multiple instances of the same role in a project.
- Durable role inboxes, connector outboxes, and append-only event journal.
- Router routes messages; it does not make workflow decisions.
- Control-plane supervises lifecycle; it does not make product, architecture,
  implementation, QA, or release decisions.
- Storage, collaboration connectors, worker/model providers, and deployment
  profiles remain adapter boundaries.
- Every deployment should emit OpenTelemetry logs, traces, and metrics.

## Certificate Of Origin

This project uses the Developer Certificate of Origin instead of a contributor
license agreement at this stage.

By contributing, you certify that you have the right to submit the contribution
under this project's license and that you agree to the Developer Certificate of
Origin 1.1:

https://developercertificate.org/

Sign off every commit:

```powershell
git commit -s
```

Your sign-off adds a line like:

```text
Signed-off-by: Your Name <you@example.com>
```

## License

Unless explicitly stated otherwise, contributions are licensed under the Apache
License 2.0, the same license as the project.

## Reporting Security Issues

Do not open public issues for vulnerabilities. Follow `SECURITY.md`.
