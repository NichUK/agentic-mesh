# AMV5-035 — Recovery deployment and PR controls

## Outcome

Turn the exact recovery job issued by AMV5-034 into one bounded repair
candidate: create an isolated Git worktree and branch, let the recovery agent
change only approved paths, test and build the committed candidate, deploy and
restart it, verify the exact running commit, push the branch, and create a pull
request. The recovery identity cannot approve or merge that pull request.

## Boundaries

- Add one small, standard-library recovery runner to the independent recovery
  image. It consumes the existing recovery-job JSON on standard input and one
  externally mounted, project-specific plan.
- Keep source, deployment roots, credentials, commands, and evidence outside
  the image. The plan names environment variables but never contains their
  values. Source, disposable workspace, and evidence roots must be separate.
- Create a disposable worktree from a pinned base revision. Reject a dirty
  source repository, an existing recovery branch, symlinks that escape the
  worktree, and every changed path outside the explicit allow-list.
- Pass the exact goal to the repair command. Do not pass source-control or
  deployment credentials to the repair command unless their variable names are
  explicitly assigned to that stage.
- Require the repair adapter to return its authoritative provider usage. Reject
  an invalid report or an over-cap result before test, build, or deployment.
- Execute commands as argument arrays without a shell. Test, build, deploy,
  restart, verification, rollback, and running-revision checks are fixed
  stages; the runner is not a general workflow engine. Project-supplied stages
  cannot invoke Git or GitHub CLI; all source-control work uses the runner's
  fixed adapter.
- Commit before building and deploy that immutable commit. After restart, the
  running-revision command must return exactly the candidate commit.
- If deployment, restart, or verification fails, run the configured rollback,
  restart, verification, and revision checks. A failed rollback is reported
  distinctly and never described as a working Mesh.
- Push only the generated recovery branch. The GitHub adapter exposes pull
  request creation only; it has no approval or merge operation. Normal release
  governance owns review and merge.
- Write a content-addressed evidence manifest outside the container. Persist
  only hashes and bounded operational facts, not command output or credential
  values. The AMV5-034 result stores its immutable evidence reference.
- Leave reusable multi-repository worktrees, project manifests, and ordinary
  release pipelines to AMV5-036, AMV5-037, and AMV5-047.

## Acceptance criteria

- An end-to-end broken fixture is repaired on a generated branch, tested,
  built, deployed, restarted, verified at the exact candidate commit, pushed,
  and submitted as a pull request.
- The evidence manifest records the base, candidate, previous deployed, and
  verified deployed commits; stage hashes; rollback state; branch; and PR URL.
- A repair that changes an unrelated path is rejected before commit, deploy,
  push, or pull-request creation.
- Failed post-deployment verification restores and verifies the previous
  commit. Failed rollback is visible and cannot produce a successful result.
- Plan files containing credential values, shell commands, GitHub CLI commands,
  or source-control mutation commands are rejected.
- Recovery can create a pull request but has no code path to approve or merge
  it. Normal release governance remains the only route to integration.
- Replaying a completed run returns the same content-addressed result without
  creating another branch, deployment, push, or pull request. Replay verifies
  the evidence digest and its project, run, exact-goal, and plan provenance.
