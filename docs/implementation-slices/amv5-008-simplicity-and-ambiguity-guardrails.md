# AMV5-008 — Simplicity And Ambiguity Guardrails

## Outcome

Block promotion of an effective role or flow prompt when it no longer tells an
agent to prefer the smallest reliable solution, reuse mature tools, minimize
handoffs, and ask the sponsor about material ambiguity.

## Scope

- keep the behavioural contract and its three bootstrap regression scenarios
  in one versioned external policy package;
- evaluate that policy deterministically during immutable release creation;
- cover a simple request, materially ambiguous intent, and a deliberately
  over-engineered proposal;
- reject missing prompt requirements, malformed scenarios, and expectations
  that disagree with the invariant; and
- leave non-prompt bootstrap releases unaffected.

This slice does not call an LLM, add an evaluation service, score free text, or
invent a general policy engine. Provider-backed behavioural evaluation can be
added after the worker-provider boundary exists. The bootstrap check protects
the exact external configuration promoted today and remains reproducible.

## Acceptance criteria

- The external policy defines exactly the simple, ambiguous, and
  over-engineered scenarios with explicit facts and expected actions.
- Simple work proceeds, material ambiguity asks the sponsor, and avoidable
  custom components or handoffs require simplification.
- Effective role/flow configuration includes all required simplicity and
  ambiguity prompt phrases before it can become an immutable release.
- Missing or malformed guardrail policy, missing prompt requirements, or an
  incorrect scenario expectation rejects release creation and writes no
  release record.
- Configuration that does not contain a role or flow remains outside this
  prompt-specific promotion gate.
- Focused tests and a clean-clone acceptance prove the three scenarios and the
  release-blocking behaviour without importing V4 modules.
