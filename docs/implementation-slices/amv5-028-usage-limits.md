# AMV5-028 — Usage, Limits, and Reset Windows

## Outcome

V5 records provider turn-token usage exactly once, exposes project totals, and
shows the latest authoritative provider capacity snapshot. Missing capacity is
reported as unknown rather than as zero.

## Boundaries

- One durable usage row is keyed by project, provider account scope, and turn.
  Repeated cumulative notifications update that row only when every counter is
  non-decreasing, so retries and concurrent delivery cannot double-count use.
- Capacity is a latest-value snapshot for the configured project/provider
  account scope. It contains only typed percentages, reset times, numeric credit
  facts, and safe provider labels.
- Codex capacity is read through the provider adapter using
  `account/rateLimits/read`; Codex response shapes do not leak into product
  semantics.
- The thread-affinity boundary refreshes capacity before work and persists turn
  usage before yielding it. Unsupported or failed capacity reads record unknown.
- No prompt, response, reasoning, OAuth identity, credential, or raw provider
  payload is stored or returned.
- Reset credits are observed, not consumed. Sponsor-approved use remains part
  of the optional self-improvement work at the end of the backlog.

## Acceptance criteria

1. Duplicate or increasing usage observations for one turn produce one total;
   decreasing or differently scoped observations fail closed.
2. Concurrent observations cannot double-count a turn.
3. Primary and secondary remaining percentages and reset times, credit facts,
   spend-control facts, and reset-credit availability are inspectable through
   the project usage API when supplied by the provider.
4. A project with no capacity observation returns `unknown`, never fabricated
   zero capacity.
5. Cross-project reads and writes remain isolated, and stored/API usage data
   contains no prompt or authentication content.

## Test plan

- Unit-test Codex SDK snapshot translation, including absent optional fields.
- Exercise idempotent, monotonic, conflicting, and concurrent turn accounting
  against Postgres.
- Exercise automatic ingestion through the thread-affinity coordinator,
  including a provider that cannot supply capacity.
- Exercise known and unknown capacity through the authenticated control API and
  verify project isolation.
- Run all V5 tests, the unchanged V4 baseline, package/secret checks, and a
  clean-clone acceptance test against real Postgres.
