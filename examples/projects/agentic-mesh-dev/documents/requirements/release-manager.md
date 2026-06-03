# Release Manager Worklist

Status: sponsor-editable adoption output

## Role View

Release management needs to protect `main`, make `develop` the tested
integration branch, and ensure runtime deployments are traceable to commits.
The current local and VM workflows are too manual for a release baseline.

## Outstanding Work

- Enforce branch model: feature branches from `develop`, PRs to `develop`,
  Copilot review, and tested promotion to `main`.
- Add release checklist covering tests, config validation, docs updated,
  dependency/license review, Docker build, Compose smoke, Teams smoke, and
  rollback notes.
- Add version/status command that reports source commit, branch, dirty state,
  project id, and runtime services.
- Add deployment notes that record which commit is running on the VM.
- Create changelog/release-notes template.
- Define release artifact set: source tag, Docker image tags, project deploy
  outputs, OSS docs, third-party notices, and smoke evidence.
- Add rollback procedure for Teams connector/Graph ingress deployment.
- Define when a dogfood runtime is stable enough to merge `develop` to `main`.

## Risks And Decisions

- Risk: Direct commits or manual copies make releases unreproducible.
  Mitigation: deploy from committed branches and record runtime commit.
- Risk: Runtime state is accidentally committed. Mitigation: keep state ignored
  and add release checks.
- Decision needed: semantic versioning start point for OSS preview.

## Suggested Acceptance Criteria

- Every release candidate has a commit id, test result, smoke result, and known
  rollback.
- `main` only advances from a tested `develop` baseline.
- The VM runtime can be reconciled against Git without guesswork.
