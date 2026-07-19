# AMV5-046 — Live role-service operationalization

## Outcome

Turn the already-qualified V5 queue, prompt, warm-engine, thread-affinity and
project-boundary components into one runnable role service, so a registered
project can progress real work without an operator claiming queue items by
hand. This closes an integration gap in the AMV5-046 arms-length acceptance
path; it does not add a second workflow engine or scheduler.

## Scope and boundaries

- Add one role-service loop for one configured project/role/instance. It claims
  only that role's durable queue, heartbeats its lease while Codex is active,
  and reuses the existing warm engine and work-item thread affinity.
- Resolve the active role package, pinned flow package and current durable flow
  state before each new work-item thread. Prompt text remains external and the
  persisted prompt digest must match the rendered package inputs.
- Prepare source work only through `ProjectRegistrationCoordinator` and the
  existing isolated-worktree coordinator. Source repositories are supplied by
  an external, secret-free runtime binding file; running install and state
  roots remain protected.
- Reuse the current external Codex OAuth cache through `CODEX_HOME`. Never copy
  auth data into Git, an image, Postgres, output, or a queue payload.
- Require the role instance to record a new structured progress checkpoint
  after its durable project effect. A completed provider turn without that
  attributable checkpoint releases the lease for recovery; it must not report
  the queue item complete.
- Complete the source queue lease only after the provider completes and the
  durable-effect check succeeds. Provider failure, heartbeat loss, workspace
  preparation failure, or missing configuration releases or abandons the
  lease through the existing retry path.
- Add one local Docker fleet supervisor that starts and stops only
  pre-provisioned containers from an external instance-to-container map.
  Container creation, secrets and project mounts remain deployment concerns.
- Add a `role-service` CLI command and install the V5 package into the existing
  project-neutral worker image. Do not add another message broker, daemon
  framework, agent protocol, prompt store, or container orchestrator.

## Testing plan

1. Run a fake worker provider through two queue items and prove one engine is
   reused for the same role instance while different work items retain distinct
   thread bindings.
2. Hold a fake turn beyond one lease interval and prove heartbeat renewal keeps
   the claim valid.
3. Prove provider completion with a new role-instance progress checkpoint
   completes the queue item exactly once, while a no-checkpoint turn releases
   it for retry.
4. Prove provider, prompt, workspace and heartbeat failures do not falsely
   complete work and redact secrets from raised and returned messages.
5. Prove project/role/instance, repository and workspace isolation fail closed.
6. Prove the Docker supervisor maps exact configured identities, performs
   idempotent start/stop, rejects unknown instances and never invokes a shell.
7. Build the worker image, run its boundary check and verify `role-service
   --help` is available without a source bind mount.
8. Run a live local queue item with the current external Codex authentication
   and confirm a durable checkpoint plus terminal provider evidence.

## Acceptance criteria

- A registered V5 project can route work to a role and have a live service
  claim, heartbeat and process it without manual queue mutation.
- One Codex engine remains warm for repeated work on the same role instance;
  work-item thread contexts remain separate and resumable.
- Queue completion is impossible without both terminal provider completion and
  a new durable project effect attributable to the work turn.
- Crash or hibernation leaves acknowledged work reclaimable through ordinary
  lease expiry and preserves thread affinity.
- Worker startup consumes only external configuration, mounted workspace/source
  boundaries, a token-file/API reference and external `CODEX_HOME`; no secret
  value appears in Git, images, database records, prompts, logs or status JSON.
- Fleet wake/hibernate acts only on the exact pre-provisioned container mapped
  to the durable project instance, and replay is safe.
- The role-service and worker image remain project-neutral and require no V4
  runtime import.
