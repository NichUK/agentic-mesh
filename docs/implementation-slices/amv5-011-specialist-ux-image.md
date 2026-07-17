# AMV5-011 — Specialist UX Image

## Outcome

Add one project-neutral UX worker image to the existing V5 worker family. The
image supplies mature browser, accessibility, screenshot, visual comparison,
image, and PDF tools. Figma connectivity remains an optional external provider
binding; no Figma credential or project material is built into the image.

## Scope

- extend the isolated `docker/v5` context with one `v5-ux-worker` target;
- install exact Playwright and axe integration versions with Chromium;
- use Playwright for browser automation, screenshots, and PDF generation;
- use axe-core for accessibility evidence, ImageMagick/pixelmatch for visual
  comparison and image inspection, and Poppler for PDF inspection;
- add a small self-contained smoke command that exercises those workflows;
- add an external `tool-profile/ux@0.1.0` package with optional provider-based
  Figma connectivity; and
- extend the existing verifier rather than introduce a second image pipeline.

This slice does not add a UX persona, Figma client implementation, runtime
scheduler, browser service, or dashboard. Credentials, prompts, memory,
projects, and generated evidence stay outside the image.

## Acceptance criteria

- The UX image builds from the existing digest-pinned V5 base and exact Codex
  version, with exact Playwright and axe integration versions.
- Chromium launches headlessly and completes a local-page workflow without an
  external service or credential.
- The smoke workflow runs an axe scan, captures and compares screenshots,
  generates a PDF, inspects the PNG and PDF, and emits structured evidence.
- The embedded image manifest and external UX profile agree on image,
  capabilities, health command, and identity.
- Figma is represented only by an optional external provider credential
  requirement; credential-free startup and smoke execution succeed.
- The build context remains allowlisted and project-neutral, and static
  verification, Compose validation, real Docker build, built-image inspection,
  and automated tests pass.
