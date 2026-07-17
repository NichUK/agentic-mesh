# AMV5-012 — Independent Recovery Image

## Outcome

Add one credential-free recovery image that can inspect, build, test, replace,
restart, and diagnose an Agentic Mesh installation. The image is excluded from
normal routing and may be launched only by the recovery supervisor.

## Scope

- extend the existing V5 worker-image pipeline with one `v5-recovery-worker`;
- include Codex, Git/GitHub CLI, Docker CLI, exact checksum-verified Docker
  Compose, build/test tools, SSH, and local network/system diagnostics;
- add a restricted external tool profile with an explicit recovery-supervisor
  launch policy and external credential/mount references;
- make launch authorization fail closed in the V5 tool-profile registry;
- keep the Compose service behind the `recovery-supervisor` profile; and
- provide a disposable drill that builds a broken fixture, observes failure,
  rebuilds it, replaces and restarts it, verifies repair, and cleans up.

The image contains no project source, prompt, memory, state, or credential. It
does not grant itself access: the later independent recovery supervisor owns
bounded invocation, goal assignment, and privilege delivery.

## Acceptance criteria

- Normal routing and ordinary launchers are rejected before the recovery image
  can be selected; `recovery-supervisor` is accepted only on the independent
  launch path.
- The image passes its embedded manifest/command health contract and contains
  no credential values or project material.
- Docker and Compose can inspect, build, replace, restart, and verify a
  disposable broken Mesh fixture through an externally mounted engine socket.
- The drill emits structured evidence and removes its containers, network, and
  temporary images on success or failure.
- Static verification, external profile resolution, Compose validation, real
  image build, built-image inspection, the real recovery drill, and automated
  tests pass.
