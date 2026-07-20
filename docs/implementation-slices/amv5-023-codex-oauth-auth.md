# AMV5-023 - Reuse Current Codex OAuth Authentication

## Purpose

Resolve a deployment-supplied named Codex OAuth cache into the external
`CODEX_HOME` environment and container mount required by a V5 worker. Reuse the
official Codex login cache; do not implement OAuth, copy credentials, or inspect
`auth.json`.

## Scope

- resolve a validated `mount_ref` from an in-memory deployment registry;
- keep host cache paths out of object representations and durable output;
- reject missing, relative, non-directory, or repository-contained caches;
- provide separate local-process and container `CODEX_HOME` environments;
- report only authenticated, sign-in-required, or wrong-method status using the
  official Codex SDK account operation;
- refresh existing ChatGPT credentials through Codex during a status probe;
- redact provider failures and close every probe process deterministically;
- allow multiple workers to resolve and use the same named cache without
  copying it;
- keep each instance's `CODEX_HOME` volume for sessions and thread state, but
  overlay the official `auth.json` from one system-scoped, read-write external
  file. Never clone a rotating ChatGPT refresh token into per-instance homes:
  one successful refresh invalidates every copied refresh token;
- require the shared auth file to exist before Compose materialisation and
  mount it at `/codex-home/auth.json` in every role container. The control
  plane and project source do not receive the file.

## Non-goals

- browser or device-code login flows;
- reading, parsing, copying, or persisting credential files;
- worker-container launch orchestration, warm-engine lifetime, or thread
  affinity;
- API-key or access-token secret resolution;
- changing the V4 authentication implementation.

## Acceptance criteria

1. A named external cache resolves to an immutable binding whose local process
   environment points at the host directory and whose container environment
   points at `/mesh/worker-auth/codex`.
2. Unknown names, unsafe names, missing paths, non-directories, relative paths,
   and caches inside configured Git/config roots fail closed.
3. Two worker instances can independently resolve the same cache and receive
   equivalent environments without any credential copy.
4. The official SDK probe refreshes existing authentication and returns only a
   non-sensitive account kind; no email, token, cache content, or provider error
   text crosses the boundary.
5. Missing or expired authentication is reported as sign-in required, while an
   API-key login is reported as the wrong method for this OAuth binding.
6. Constructor, binding, status, exception, test, log, and documentation output
   contain no credential material. The cache path is excluded from `repr`.
7. An explicit local acceptance test using the current external `CODEX_HOME`
   confirms the existing ChatGPT login without reading credential files.
8. All LinuxCH role instances mount the same external `auth.json`, while their
   16 external `CODEX_HOME` volumes remain distinct. Replacing or refreshing
   the shared file is immediately visible from two different role containers.
9. A live login/model probe succeeds from two role identities in sequence, and
   neither reports refresh-token reuse, token expiry, or a copied credential.

## Test plan

- unit-test valid and invalid mount resolution, forbidden roots, symlinks,
  environment generation, immutable snapshots, and redacted representations;
- use fake official-SDK clients to test multiple workers, refresh requests,
  authenticated, expired, wrong-method, startup, account, and close failures;
- run an opt-in real SDK status probe against the current external cache;
- rerun the AMV5-022 provider, worker-image, and V5 runtime-boundary suites;
- scan the change set and built wheel for accidental credential files or values.
