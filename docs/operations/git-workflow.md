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
python -m agentic_mesh.cli validate-config
git push -u origin codex/<slice-name>
```

Check the PR size before pushing:

```powershell
python scripts/check-pr-size.py --base origin/develop --committed-only
```

Then open a pull request targeting `develop` and request Copilot review.
