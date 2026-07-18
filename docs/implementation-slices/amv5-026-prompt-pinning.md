# AMV5-026 - Prompt-Version Pinning and Controlled Reseeding

## Purpose

Pin every durable work conversation to the immutable effective configuration
digest that created its provider thread. Configuration activation must not
silently alter active or existing work; moving a conversation to another prompt
version requires an explicit, recorded, idle-only reseed.

## Scope

- extend durable thread affinity with prompt digest, generation, state, and
  active-operation ownership;
- require a 64-character effective configuration digest for every new binding
  and every thread operation;
- fail closed when a caller presents a different active configuration digest;
- serialize use of one affinity across role instances with a durable operation
  token, acquired atomically with first/post-reseed thread binding and released
  after the complete thread operation;
- preserve upgraded pre-pin bindings as `unpinned` and refuse to resume them;
- add immutable reseed history containing old provider/thread/digest, new
  digest, generation, actor, reason, and timestamp;
- require expected-current digest, no active operation, and a changed target
  digest before reseeding;
- transition a reseeded affinity to `pending_seed` with no provider thread;
- let the next real work operation create and bind the new thread under the new
  digest, preserving old context only in reseed history.

## Non-goals

- deciding which release should be activated or approved;
- changing immutable configuration release content;
- automatically reseeding after activation, rollback, hibernation, or failure;
- copying conversation history into a new provider thread;
- clearing an operation left active by a crashed process; recovery supervision
  must resolve that fail-closed condition explicitly;
- queue routing, progress checkpoints, or sponsor approval UX.

## Acceptance criteria

1. A new affinity stores the supplied effective configuration digest and
   generation one; subsequent operations must present the identical digest.
2. Activating another configuration does not change an existing affinity or
   provider thread. Work using its pinned digest continues; presenting the new
   digest fails until a controlled reseed occurs.
3. One affinity permits one durable active operation across all same-role
   instances. Concurrent use or reseed while active fails closed.
4. Successful and failed in-process operations release their exact operation
   token. A stale token cannot release another operation.
5. Reseed requires actor, reason, expected current digest, and a different valid
   target digest, and is rejected while active or on optimistic mismatch.
6. Reseed history is immutable and source-linked by affinity key and generation.
   It records identifiers only, never prompt text or private thread content.
7. After reseed the old thread cannot be resumed. The next operation using the
   new digest creates a distinct thread and activates the next generation.
8. Existing unpinned upgrade rows cannot run silently but may be moved through
   the same explicit reseed operation.
9. Hibernation, engine failure, or global config changes do not alter a pinned
   digest or trigger reseeding.

## Test plan

- test clean and populated upgrades, database state checks, composite role
  authorization, maintenance guards, and backup/restore;
- test first bind, same-digest resume, digest mismatch, configuration activation,
  hibernation, and engine replacement;
- coordinate concurrent same-affinity operations and active reseed attempts;
- inject operation exceptions and stale release tokens;
- test optimistic reseed, immutable history, pending seed, new-thread activation,
  and legacy unpinned recovery;
- run a current-auth Codex first-turn/reseed/resume acceptance without storing
  project prompts or credentials;
- rerun the complete V5 suite with real Postgres.
