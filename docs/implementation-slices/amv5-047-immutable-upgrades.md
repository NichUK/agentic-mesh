# AMV5-047 — Immutable V5 build and upgrades

## Outcome

Build and verify a V5 candidate from an exact clean Git commit, deploy only its
content-addressed image, health-check it, and retain or restore the previous V5
image on failure. Live code is never supplied through a source bind mount.

## Scope and boundaries

- Add one durable release coordinator; do not create a second workflow engine.
- Keep image building and deployment behind small provider-neutral ports. The
  first local adapters use Docker and Docker Compose without a shell.
- Persist immutable candidate facts and idempotent deployment attempts in
  Postgres before deployment side effects.
- Require a clean source checkout at the requested commit, successful configured
  tests, a successful image smoke probe, and a digest-addressed image reference.
- Require the current database schema to be supported by both the candidate and
  rollback image. Do not apply database migrations implicitly during deployment.
- Compare the observed deployed image with the registered boundary before an
  upgrade. Drift fails closed.
- Update the registered running image only after candidate health succeeds.
  Failed candidate health restores and verifies the previous image.
- Reconcile an interrupted attempt from observed deployment state. Never repeat
  a completed deployment operation or guess when state is unhealthy/unknown.
- Store bounded evidence hashes and references, not build logs, credentials, or
  environment values.
- Keep the runtime Dockerfile and Compose output project-neutral and
  project-owned respectively. Compose may mount external configuration and
  runtime state, but never the Agentic Mesh source checkout.

## Acceptance criteria

- A real V5 runtime image contains installed V5 code and passes its image smoke
  probe without a source mount.
- Exact operation replay returns the recorded outcome without another build or
  deployment.
- Candidate preflight failure leaves the previous deployment untouched.
- Candidate post-deployment health failure restores and verifies the previous
  V5 image; a rollback failure remains explicit and never reports success.
- Compatible schema versions deploy; incompatible candidate or rollback schema
  ranges are rejected before deployment.
- Restart reconstruction can finish a healthy candidate or a verified rollback
  from the durable in-progress attempt.
- Evidence identifies the project, story/operation, source commit, candidate
  image digest, previous image digest, schema version, checks, and final outcome.
