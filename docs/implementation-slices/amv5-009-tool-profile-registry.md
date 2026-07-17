# AMV5-009 — Tool-profile Registry

## Outcome

Load an external, versioned tool profile into a small category-neutral runtime
contract. A profile declares its worker image, capabilities, mounts, credential
requirements, health check, and default resources without embedding credentials
or project data.

## Scope

- define and validate the version-one external tool-profile content contract;
- publish a new general profile version without changing the released bootstrap
  profile;
- load one exact tool-profile package through the existing deterministic package
  resolver;
- expose typed, immutable profile values and an explicit capability check; and
- prove a synthetic future corporate-agent profile works with the same schema
  and runtime code.

Container builds, secret delivery, health-check execution, scheduling, resource
enforcement, and a dynamic plugin system belong to later stories.

## Acceptance criteria

- A valid profile declares an image, capabilities, mounts, credential
  requirements, health check, and positive resource defaults.
- Profile identifiers and capability names are data, not enumerated role or
  agent categories.
- Profile identity must match its package reference; duplicate identifiers,
  unsafe mount targets, malformed health settings, and embedded image
  credentials are rejected with useful errors.
- A caller can ask whether the loaded profile supplies required capabilities
  without knowing the profile category.
- Automated tests load the general profile, reject malformed contracts, and
  create and load a synthetic finance profile without changing the schema or
  V5 registry implementation.
