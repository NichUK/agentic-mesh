# Technical Writer Worklist

Status: sponsor-editable adoption output

## Role View

Documentation is already valued as a product artifact, but it needs a clearer
reader path. The repo has many good design documents; users need a quickstart,
operator guide, contributor guide, and architectural map that keep the project
understandable as it grows.

## Outstanding Work

- Rewrite README for public preview: what Agentic Mesh is, who it is for,
  current maturity, quickstart, demo path, architecture links, and commercial
  boundary.
- Add "First Local Project" quickstart using project-scoped Compose.
- Add "Teams Setup" guide covering app registrations, scopes, secrets,
  channels, mentions, and troubleshooting.
- Add "All-Agents Directives" guide explaining direct work versus lifecycle
  handoff, acknowledgements, and expected artifacts.
- Add "Project Configuration" examples for one role, multiple role instances,
  connectors, workspace repositories, and write paths.
- Add "Operations" guide for status, queues, cursors, connector outbox,
  config reload, hibernation, and logs/traces.
- Add "Contributing A Role Pack" guide.
- Add "Open Core And Commercial Boundary" page from the product plan.
- Add docs freshness policy: update ADRs/design docs when behavior changes.

## Risks And Decisions

- Risk: Docs describe future design as if implemented. Mitigation: label docs
  with status and current capability.
- Risk: Users cannot tell system repo from project repo. Mitigation: make the
  boundary visible in quickstart diagrams and examples.
- Decision needed: docs structure for public website versus repository docs.

## Suggested Acceptance Criteria

- A new user can run the local demo by following docs only.
- An operator can diagnose "nothing happened" from status, queues, cursors, and
  logs without reading source code.
- Every public-facing doc names whether it is implemented, draft, or backlog.
