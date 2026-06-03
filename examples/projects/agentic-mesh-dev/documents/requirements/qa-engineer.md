# QA Engineer Worklist

Status: sponsor-editable adoption output

## Role View

The current tests cover important units, but the adoption failure proves that
end-to-end behavior needs scenario tests with negative assertions. QA should
verify not only that messages route, but that they do not route when they are
acknowledgements, echoes, plain text, or non-work chatter.

## Outstanding Work

- Add an all-agents scenario test: one real mention creates one direct work
  item and one inbox message per role.
- Add negative tests for acknowledgement echo, bot-authored messages, plain
  `all-agents` text without mention, duplicate messages, and empty messages.
- Add end-to-end smoke test for Teams post to docs artifact creation with no
  lifecycle handoffs and no approvals.
- Add quality gate that blocked worker runs cannot satisfy tasks requiring role
  analysis and that completed worker runs include artifact or handoff evidence.
- Add project build output tests proving generated files remain inside the
  project folder.
- Add concurrency tests for multiple Engineering instances claiming from the
  same role queue.
- Add config reload smoke test for listener/Graph ingress containers.
- Add release checklist for local Windows tests, Linux VM tests, Compose smoke,
  and Teams smoke.
- Add fixtures for sanitized Graph channel messages including mentions,
  application-authored messages, Adaptive Card replies, and deleted/edited
  messages.

## Risks And Decisions

- Risk: Passing unit tests miss operational loops. Mitigation: add scenario and
  replay tests against captured raw connector payloads.
- Risk: Manual Teams testing is brittle. Mitigation: preserve raw payloads as
  fixtures and replay them through ingress adapters.
- Decision needed: minimum smoke evidence required before merging `develop` to
  `main`.

## Suggested Acceptance Criteria

- A replay test reproduces this all-agents incident and proves it stays fixed.
- `pytest -q` plus one VM smoke test is enough evidence for `develop` merges.
- QA can inspect docs, journal events, and queue states for each critical flow.
