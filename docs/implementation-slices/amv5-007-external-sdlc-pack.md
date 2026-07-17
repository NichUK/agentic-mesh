# AMV5-007 — External SDLC Role And Flow Pack

## Outcome

Provide the initial 15-role software-delivery pack and its complete SDLC flow
as versioned external configuration, then render a deterministic effective
prompt for any role in any flow state without importing V4 runtime code.

## Scope

- rewrite useful role purpose, accountability, decision, collaboration, memory,
  documentation, and handoff knowledge into external role packages;
- preserve the proportionate TOGAF-aligned 14-state SDLC flow, consultations,
  owner reviews, sponsor gates, and conditional architecture route;
- make the Project Manager the continuation leader while keeping specialist
  decisions with their accountable roles;
- validate role and flow content, references, and package dependencies in the
  external repository;
- resolve the selected role and flow through the existing deterministic package
  resolver; and
- render state-specific prompt text with its package digest and provenance.

This slice does not implement the flow engine, queues, workers, databases,
memory service, or behavioural promotion tests. Those remain in their ordered
stories. It ports role knowledge, not V4 execution mechanics.

## Acceptance criteria

- The external repository contains exactly the 15 approved SDLC role IDs and
  one versioned `flow/sdlc` package with all 14 approved states.
- Every role package inherits the external core, security, and simple-delivery
  packages, including the instruction to avoid over-engineering and ask the
  sponsor when material intent is ambiguous.
- Role and flow schemas reject malformed content, unknown roles, unknown state
  targets, duplicate gate/route identifiers, and incomplete terminal rules.
- The flow identifies `project-manager` as continuation leader and permits work
  to stop only at a sponsor gate, terminal completion, or terminal error after
  the required correction and independent-recovery chain.
- V5 renders every role/state combination deterministically from external
  packages, including role ownership, consultations, gates, routes, artifact,
  package digest, and source provenance.
- Automated checks reject V3/V4 runtime mechanics in the new package content,
  and no V5 module imports a V4 module.
- A clean-clone acceptance run validates both repositories and renders all
  210 role/state combinations successfully.
