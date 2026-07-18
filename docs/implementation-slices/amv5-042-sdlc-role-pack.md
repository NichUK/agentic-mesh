# AMV5-042 — SDLC role-pack activation

## Outcome

Materialize an active project manifest's externally configured roles as one durable logical
project-role each, with a queue, bounded instance pool, scaling policy, prompt/flow binding,
tool profile, collaboration identity, and project-role memory scope.

## Boundary

- The project manifest explicitly selects each role package, tool profile, and instance range.
  Runtime code does not derive a profile from a role name or class.
- Activation validates every package and profile before changing Postgres, verifies that the
  complete flow role graph is configured, and then applies the pack in one project-locked
  transaction.
- One role binding and queue represent the logical project-role. Concrete instances reference
  that role and therefore share its project-role memory; no persona or prompt is copied into an
  image or instance record.
- The configured maximum creates a dormant instance pool. Existing fleet scaling wakes minimum
  capacity or zero-sized roles with queued work and hibernates idle specialists.
- Collaboration identity is a stable logical role identity. Later connector stories map it to
  provider-specific bot credentials without changing role semantics.
- Prompt configuration is stored as package references and immutable digests. Work-item threads
  continue to pin their effective prompt separately.
- Roles removed by a later active manifest stop receiving routes; retained historical instances
  and records are not destructively deleted.
- Do not add fifteen executors, queue implementations, worker images, or role-name conditionals.

## Acceptance criteria

- All 15 released SDLC roles validate, bind, route, and can be woken through the existing fleet
  path.
- Package identity, flow coverage, normal-routing profile authorization, project ownership, and
  instance bounds fail closed before partial activation.
- Multiple instances reference one logical role binding, queue, prompt configuration, and
  project-role memory scope.
- A synthetic future role activates through the same manifest/package/profile contract without a
  control-plane or database-schema change.
- Repeating the same activation creates no duplicate roles, queues, bindings, policies, or
  instances.
