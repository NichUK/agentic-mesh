# AMV5-037 — Isolated multi-repository Git worktrees

## Outcome

Give each mutating V5 work item one durable workspace containing an isolated
Git worktree for every selected repository in its active project manifest.
Agents receive only those worktree paths; live source checkouts and user edits
are never cleaned, reset, or used as mutation targets.

## Scope and boundaries

- Use standard Git worktrees through one small coordinator. Do not create a
  source-control service or duplicate Git behavior.
- Resolve repository ids, canonical URLs, and default branches from the active,
  immutable project-manifest snapshot. A caller supplies the corresponding
  local source checkout for each selected id; its configured `origin` must
  match the manifest.
- Require an absolute external workspace root that neither contains nor is
  contained by a source checkout. Preserve dirty source checkouts unchanged.
- Create deterministic `codex/<project>-<work-item>` branches and
  `<root>/<project>/<work-item>/<repository>` worktree paths. Reject unsafe ids,
  pre-existing unowned paths, branch mismatches, and repository substitutions.
- Persist the plan before Git side effects. Record each repository's source,
  URL, base commit, branch, worktree path, state, current head, and error so
  work links directly to reproducible repository evidence.
- Serialize one work item and each involved source repository with sorted
  Postgres advisory locks. Different work items remain isolated while Git
  metadata mutations on a shared source repository cannot race.
- Resume a partially prepared workspace only when existing worktrees and
  branches match the durable plan. Any mismatch fails closed without deleting
  files.
- Cleanup removes only registered, clean worktrees. It retains branches and
  evidence. Dirty worktrees are marked cleanup-blocked and left intact for
  recovery or handoff; a later clean retry completes cleanup.
- Do not fetch, push, merge, reset, clean, delete branches, or inject source
  credentials in this slice. Those actions remain owned by delivery flows and
  later adapters.

## Acceptance criteria

- A work item can prepare one workspace across multiple manifest repositories,
  and an exact retry returns the same paths, branches, and base commits.
- Concurrent work items use different worktree paths and branches; a change in
  one cannot appear in another or in the source checkout.
- Dirty and untracked user files in source checkouts survive preparation and
  cleanup byte-for-byte.
- A partial/crashed prepare can be reconciled from its durable plan, while an
  unexpected path, URL, branch, owner, or commit is rejected without deletion.
- Cleanup removes clean registered worktrees, preserves their branches and
  final-head evidence, and leaves dirty worktrees in a visible blocked state.
- Postgres records tie every repository path, URL, branch, base revision,
  current revision, status, and error to the exact project and work item.
