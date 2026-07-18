# AMV5-027 - Safe Progress Checkpoints

## Purpose

Record role progress as explicit, safe, project-scoped fields so operators and
the dashboard can show useful live state without parsing model text or running
another summarizer model.

## Scope

- reuse the existing V5 `progress` table and live-read projection;
- add a caller-supplied checkpoint id for idempotent retries;
- append checkpoints with an expected-previous-sequence precondition;
- record project, work item, role instance, status, goal, step, completed
  action, current activity, blocker, next action, and safe summary;
- serialize checkpoint creation on the work item so concurrent writers cannot
  produce duplicate or out-of-order sequence numbers;
- expose one authenticated project-write API operation and the existing
  structured progress read operation;
- reject empty/oversized fields, common credential material, private keys,
  credential-bearing connection strings, and explicit private-reasoning tags
  before opening a database transaction;
- return redacted durable-store failures without echoing rejected content.

## Non-goals

- generating or rewriting the safe summary;
- parsing free-form model output;
- storing prompts, messages, private reasoning, credentials, or tool output;
- deciding work lifecycle transitions, handoffs, queue routing, or terminal
  completion;
- adding a new queue, service, model call, or progress table;
- implementing the worker safe-output transport introduced with later routing
  stories.

## Acceptance criteria

1. A valid checkpoint stores every structured field and immediately appears in
   the existing progress API/read model with the supplied safe summary intact.
2. The store assigns the next sequence only when `expected_previous_sequence`
   matches the current sequence. Stale and concurrent writers fail closed
   without a partial row or sequence gap.
3. Repeating the same checkpoint id and payload returns the original record;
   reusing that id with different content is rejected.
4. The work item and concrete role instance must exist in the same project.
   Foreign-project or unknown identifiers cannot create progress.
5. Required text is non-empty and bounded; optional text is either absent or
   non-empty and bounded. Status uses a small machine-readable slug.
6. Credential/key patterns, credential-bearing database URLs, and explicit
   private-reasoning tags are rejected in every text field before persistence.
   Errors never repeat the rejected value.
7. Safe summary is caller supplied and stored directly. Product code performs
   no free-text parsing and imports no provider or summarizer implementation.
8. The operation respects the database maintenance pause, produces the normal
   live-read event in the same transaction, and survives backup/restore through
   the existing database tooling.

## Test plan

- migrate clean and populated databases and verify deterministic legacy
  checkpoint ids;
- record complete and minimal checkpoints and inspect database/read-model/API
  output;
- test stale, concurrent, idempotent, conflicting-id, wrong-project, and
  unknown-resource cases;
- parameterize sensitive content across every text field and verify no row;
- inject database failure and maintenance pause and verify redacted errors;
- assert the implementation has no provider/summarizer dependency or free-text
  parsing path;
- rerun the complete V5 suite with real Postgres and the unchanged V4 baseline.
