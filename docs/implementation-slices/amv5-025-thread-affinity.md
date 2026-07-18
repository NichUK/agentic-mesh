# AMV5-025 - Work-Item Thread Affinity and Resumption

## Purpose

Bind one durable provider thread to each project/work-item/role/conversation and
resume it across turns, engine replacement, hibernation, and same-role instance
changes. Unrelated work and roles must never share provider context.

## Scope

- add a V5 migration for durable thread-affinity records;
- key affinity by project, work item, logical role, and conversation id;
- exclude concrete role-instance id from the key so another instance of the
  same role can resume the logical agent's context;
- verify the executing instance belongs to the key's project and role;
- serialize first-thread creation for one affinity key with a database advisory
  transaction lock;
- make provider/thread identity globally unique across projects;
- start a persistent provider thread once and resume its recorded id later;
- execute the caller's complete thread operation while the warm instance engine
  remains exclusively checked out;
- fail closed on provider mismatch, missing context, or cross-project/role use.

## Non-goals

- prompt/package version pinning or controlled reseeding;
- turn persistence, progress checkpoints, routing, queue retry, or autoscaling;
- automatic replacement when a recorded provider thread is missing;
- parsing provider thread history or copying private conversation content;
- connector conversation discovery.

## Acceptance criteria

1. The database stores exactly one active thread binding for each
   project/work-item/role/conversation key.
2. Two concurrent first-use attempts call provider thread creation once and
   return the same recorded binding.
3. Repeated operations resume the recorded thread rather than starting another
   thread, while a different work item, role, conversation, or project gets a
   distinct binding.
4. A hibernated or failed warm engine is replaced and resumes the same durable
   provider thread.
5. A different instance of the same project role can resume the binding; an
   instance from another role or project is rejected before provider use.
6. One provider/thread id cannot be bound to two affinity keys, including keys
   in different projects.
7. Persistent affinity rejects ephemeral thread requests. Missing or rejected
   provider threads fail without silently creating a fresh context.
8. Stored records and runtime errors contain identifiers and timestamps only,
   never project prompts, messages, credentials, or private thread content.

## Test plan

- migrate clean and upgraded Postgres databases and inspect keys, constraints,
  foreign keys, maintenance guards, and backup compatibility;
- run concurrent bind attempts against Postgres and count provider starts;
- use provider-neutral fake engines with the warm-engine pool for repeated
  turns, hibernation, engine failure, and same-role instance takeover;
- probe work-item, role, conversation, project, and provider/thread isolation;
- inject database, provider, authorization, and malformed-id failures and
  verify redacted fail-closed behavior;
- rerun migration, backup, warm-engine, Codex provider/auth, and V5 boundary
  suites.
