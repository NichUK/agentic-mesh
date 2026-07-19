# Project Import Completeness Questions

AMV5-054 turns one pinned AMV5-053 discovery report into a durable,
restart-safe question loop. The loop collects material project intent,
ownership, sponsor, source access, source purpose, unresolved discovery, and
deployment facts before AMV5-055 may construct a preview.

One Postgres session stores the safe discovery report, immutable initial
question set, current answers, status, and optimistic version. An append-only
event table records every follow-up question set and material answer round.
The Project Manager may add a concrete follow-up when an answer reveals a new
material ambiguity; doing so moves a ready session back to questioning.

Answers are structured by question kind. Credential questions accept only an
external `secret://`, `mount://`, or `oauth-cache://` reference, or the exact
`none-public` declaration. Credential values are not valid answers, and common
embedded-secret markers are rejected in other persisted text. Repeating an
identical answer is idempotent. A different answer conflicts unless it is
submitted as an explicit correction with a reason; both the old value and the
correction remain visible in the append-only event history.

The readiness operation requires the caller's expected session version and
returns a digest-pinned resolution only when every material question has an
answer. It does not generate a manifest, backlog candidate, resource claim, or
activation. Those operations remain AMV5-055.

## Acceptance Criteria

- Sessions and all material rounds survive process restarts in Postgres.
- Initial questions cover missing project identity, intent, ownership,
  sponsors, every declared source, discovery problems, and deployment facts.
- The Project Manager can add source-linked follow-up questions without
  changing or deleting earlier questions or answers.
- Partial answers across multiple optimistic-concurrency rounds leave the
  session in `questioning` until all material questions are answered.
- An unmarked contradictory answer fails atomically; an explicit correction
  requires a reason and leaves append-only evidence.
- Unknown questions, stale versions, malformed values, embedded credential
  values, and concurrent changes fail closed.
- Preview readiness is version-pinned and impossible while a question remains
  unanswered.
- No source system or active project record is mutated, and no project-specific
  import path exists.
