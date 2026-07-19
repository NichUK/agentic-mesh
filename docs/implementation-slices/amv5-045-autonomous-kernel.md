# AMV5-045 — Autonomous kernel qualification

## Outcome

Prove that a small, preconfigured project can complete a representative SDLC
flow through the versioned control API and bootstrap CLI without direct queue
repair or in-process workflow calls. The qualification is the entry criterion
for registering Agentic Mesh V5 as an arms-length dogfood project.

## Scope and boundaries

- Expose the existing external-flow and governance operations through the
  authenticated `/api/v1` boundary: start/read, obligation dispatch and read,
  artifact verification, consultation and owner-gate evidence, transition
  preparation/pickup, and terminal completion.
- Resolve the already-validated, package-pinned flow and the project-scoped
  `DocumentStore` through injected adapter functions. The API does not accept
  an arbitrary flow definition or a filesystem document path.
- Derive the acting role from the authenticated principal. Do not accept a
  caller-supplied role identity for governance decisions.
- Keep the existing explicit handoff protocol. Source completion remains
  blocked until the target lease claims and accepts the handoff.
- Treat project registration, role-pack activation, worker-image launch, and
  external Git/document writes as project setup or agent work. The measured
  kernel progression uses only API calls and the sponsor-decision CLI.
- Use one compact qualification flow covering product definition, development,
  QA, release, a sponsor gate, consultation, documents, a real Git commit,
  structured progress, and terminal evidence. This is qualification evidence,
  not a second production flow or special demo endpoint.
- Recreate the API application during active work, allow a worker lease and a
  PM-monitor lease to expire, and reclaim an expired target handoff lease. The
  normal claim paths must recover all three cases without SQL queue repair.

## Testing plan

1. Start the compact flow through the API, verify the product artifact, open a
   sponsor gate, and approve it through the bootstrap CLI.
2. Complete each source-to-target transition through route, target claim,
   handoff claim/accept, source completion, and flow pickup.
3. Recreate the API during active work and take over expired worker and global
   PM-monitor leases through their normal API operations.
4. Expire a claimed target lease, reclaim it with a replacement instance, and
   accept the same handoff exactly once.
5. Verify all state artifacts, consultation and sponsor evidence, structured
   progress, accepted handoffs, Git commit evidence, release evidence, and
   completed work/flow state.
6. Verify missing flow/document adapters fail closed and governance role
   identity cannot be spoofed in request payloads.

## Acceptance criteria

- The work item and pinned flow reach verified terminal completion using only
  the control API and sponsor CLI after project setup.
- Every handoff is accepted before its source lease completes; an expired
  target lease is recovered through ordinary claim operations.
- API, worker, and PM-monitor restarts preserve durable progress and create no
  duplicate continuation.
- Product, development, QA, and release documents exist with immutable eTag
  evidence; the final evidence names the exact Git commit and test/release
  results.
- The evidence review finds no ready/leased orphan for the completed work and
  no manual queue mutation was required.
- The autonomous kernel is suitable for AMV5-046 arms-length registration.
