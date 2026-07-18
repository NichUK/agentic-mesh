# AMV5-024 - Keep One Codex Engine Warm per Role Instance

## Purpose

Keep one provider engine alive across successive turns for each durable
project/role-instance identity. Avoid app-server startup on every queue item
without introducing autoscaling, queue dispatch, or thread-affinity policy.

## Scope

- key warm engines by both project and role-instance id;
- lazily open exactly one engine for a role instance;
- serialize operations within one role instance while allowing different
  instances to run concurrently;
- reuse the engine across successful turns and idle periods;
- expose safe use-count and idle-duration snapshots for later hibernation logic;
- evict and close an engine after transport/protocol failure or explicit
  supervisor discard;
- close an idle engine only through explicit hibernate or pool shutdown;
- retry one failed close and return only safe lifecycle errors.

## Non-goals

- durable thread affinity or thread resumption;
- queue claims, routing, retries, autoscaling, or idle timers;
- starting or stopping worker containers;
- persisting provider objects or process identifiers;
- deciding whether provider request/auth/capacity errors should be retried.

## Acceptance criteria

1. Repeated successful operations for one project/role instance use the same
   engine and open the provider once.
2. The same instance id in different projects receives different engines.
3. Concurrent operations for one instance are serialized; operations for
   different instances can proceed concurrently.
4. Idle time does not restart or close an engine. A safe snapshot reports its
   completed-use count and monotonic idle duration.
5. Explicit hibernation waits for an active operation, closes the engine, and
   causes the next operation to open a replacement.
6. Transport/protocol failure or explicit discard evicts the failed engine;
   invalid-request, capacity, auth, and consumer exceptions do not silently
   replace it.
7. Shutdown attempts to close every engine. Close is retried once, errors are
   redacted, and stale failure/discard calls cannot evict a replacement.

## Test plan

- use provider-neutral fake providers and engines to count opens, uses, and
  closes;
- use deterministic clocks to prove idle reuse and snapshot values;
- coordinate threads with events to prove per-instance serialization,
  cross-instance concurrency, and hibernate waiting;
- inject every provider error category plus generic consumer errors;
- inject first-close and repeated-close failures and verify safe cleanup;
- rerun Codex provider/auth, worker-image, and V5 boundary coverage.
