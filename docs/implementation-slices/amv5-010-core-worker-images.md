# AMV5-010 — Core Worker Images

## Outcome

Build project-neutral general, development, QA, and operations worker images
from one small shared base. Images contain tools and capability metadata only;
role prompts, memory, projects, credentials, and runtime state stay external.

## Scope

- add an isolated `docker/v5` build context without changing the active V4
  Dockerfile or Compose deployment;
- pin the shared Python base by digest and Codex CLI by exact version;
- provide four image targets and corresponding external tool profiles;
- embed a project-neutral image manifest and healthcheck in each target;
- verify target/profile consistency, required commands, labels, build-context
  isolation, and absence of credential material; and
- build and inspect all four images with Docker.

Browser, accessibility, visual, image, and PDF tooling belongs to AMV5-011.
Independent repair tooling and elevated recovery access belongs to AMV5-012.
This slice does not add a scheduler, worker process, credential delivery, or
project-specific dependencies.

## Acceptance criteria

- `general`, `development`, `qa`, and `operations` build from the same
  digest-pinned minimal base and exact Codex version.
- Every image passes its embedded healthcheck and supplies every command and
  capability declared by its image manifest and external tool profile.
- The build context contains only the Dockerfile, healthcheck, and image
  manifests; it contains no source repository, prompt, memory, project, state,
  or credential files.
- External image/profile identifiers are exact and no profile contains a
  credential value.
- Static verification, build-context credential scanning, Docker Compose
  validation, four Docker builds, built-image inspection, and automated tests
  pass.
