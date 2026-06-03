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

Each pull request should:

- describe the product or implementation slice
- include test or smoke evidence
- call out configuration, deployment, or operational impact
- request GitHub Copilot review before merge
- keep unrelated changes out of the branch where practical

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

Then open a pull request targeting `develop` and request Copilot review.
