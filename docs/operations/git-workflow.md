# Git Workflow

Status: active dogfood workflow

Date: 2026-06-03

## Branches

`develop` is the daily integration branch for Agentic Mesh.

`main` is the stable branch. Changes should not be committed directly to
`main` during normal development.

Feature, fix, spike, and documentation work should branch from `develop` using
the `codex/` prefix for Codex-created branches, for example:

```text
codex/workspace-repo-config
codex/teams-card-update
codex/otel-trace-model
```

## Pull Requests

Feature branches should be pushed to GitHub and opened as pull requests back
to `develop`.

Pull requests must be small enough for human review and automated review. A PR
that GitHub Copilot cannot review because of size is too large to merge as one
unit.

Each pull request should:

- describe the product or implementation slice
- include test or smoke evidence
- call out configuration, deployment, or operational impact
- request GitHub Copilot review before merge
- keep unrelated changes out of the branch where practical

Review size policy:

- Preferred: one focused slice, normally under 1,500 changed lines and under
  25 files.
- Hard stop: do not merge PRs over 5,000 changed lines or 50 files unless the
  branch only contains generated/vendor artifacts and the exception is written
  in the PR.
- Never use a large PR to batch unrelated fixes, docs, runtime changes,
  generated state, and deployment changes together.
- Split large work into stacked or sequential PRs: foundations, schema/config,
  runtime code, UI/docs, tests, and deployment can usually be reviewed
  separately.
- If Copilot review is refused because the PR is too large, split the PR before
  merge.

Merges from `develop` to `main` should happen only when the current dogfood
runtime is stable enough to mark a useful baseline.

## Local Branch Setup

If the repository has no `develop` branch yet:

```powershell
git switch main
git switch -c develop
```

For new work:

```powershell
git switch develop
git pull --ff-only
git switch -c codex/<slice-name>
```

When the slice is ready:

```powershell
pytest -q
agentic-mesh --db .tmp/v2-pr.sqlite3 init-db
agentic-mesh --db .tmp/v2-pr.sqlite3 demo-slice
agentic-mesh --db .tmp/v2-pr.sqlite3 status-json
git push -u origin codex/<slice-name>
```

Check the PR size before pushing:

```powershell
python scripts/check-pr-size.py --base origin/develop --committed-only
```

Then open a pull request targeting `develop` and request Copilot review.

## Runtime Git Identity

The dogfood deployment uses one already-approved host SSH identity for Git
transport. Set `AGENTIC_MESH_GIT_SSH_HOST_PATH` to its host directory (on
linuxch this is `/home/nich/.ssh`). Compose mounts that directory read-only for
full-authority Engineering and promotion roles; role startup copies it into the
ephemeral container with OpenSSH permissions.

The linuxch release script verifies `ssh -T git@github.com` before changing the
running fleet. A missing, unregistered, or unusable identity therefore blocks
deployment explicitly instead of allowing Engineering to discover the problem
after implementation. The legacy
`AGENTIC_MESH_PROJECT_MANAGER_SSH_HOST_PATH` setting remains a compatibility
fallback but should not be used for new deployments.

Git transport authentication does not authorize GitHub API operations. Store
the approved repository-scoped token as a single value in the host secret file
referenced by `AGENTIC_MESH_GITHUB_TOKEN_FILE_HOST_PATH` (on linuxch this is
`/home/nich/agentic-mesh-projects/agentic-mesh-dev/state/secrets/github-token`).
The file is mounted read-only only for full-authority Engineering and promotion
roles; container startup exports it as `GH_TOKEN` and `GITHUB_TOKEN` without
placing the value in Compose, project configuration, logs, or Git.

The release preflight refuses to recreate the fleet unless the token
authenticates to the GitHub API and has write access to `NichUK/agentic-mesh`. This ensures a
role that can push a branch can also open a pull request, request review, and
inspect checks without asking the sponsor to supply credentials again.
