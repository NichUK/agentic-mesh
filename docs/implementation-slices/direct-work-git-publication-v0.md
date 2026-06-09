# Direct Work Git Publication V0

Status: backlog slice

Date: 2026-06-03

## Context

All-agents/direct sponsor work is project work. The generated artifacts should
not stay as loose files on a runtime VM. For Git-backed projects, each direct
work item should have a publication boundary that can be reviewed, committed,
pushed, and merged like other project changes.

## Current Slice

Direct broadcast intake now assigns a `git_branch` and `publication` block to
every role message for the work item. Role started/completed status messages
include the same branch. When every requested role has reached a terminal
queue state, the runtime emits a single `sponsor_directive.publish_ready`
connector message with the branch and artifact paths.

## Target Behaviour

For a Git-backed project repository:

1. Intake creates or checks out the work branch before fan-out.
2. Every role writes artifacts on that branch.
3. The runtime waits until all requested roles are terminal.
4. The publication service stages configured artifact paths.
5. The publication service commits with the work item id in the subject/body.
6. The publication service pushes the branch.
7. Where configured, it opens a pull request back to the project default branch.

## Acceptance Criteria

- A direct `@all-agents` message creates one deterministic branch name for the
  work item.
- Branch creation/check-out is protected by a per-repository lock so concurrent
  workers cannot switch the same worktree mid-run.
- The publication service refuses to commit untracked or modified files outside
  the work item's allowed artifact paths.
- The final Teams message links the branch or pull request.
- If push or PR creation fails, the work item is reported as publish-blocked
  with the exact Git error and recovery command.
- Non-Git artifact targets, such as OneDrive or SharePoint, can be configured
  later without changing role worker output.

## Open Questions

- Should direct work branches be created from `develop`, from the repository
  declared `default_branch`, or from the currently checked-out branch?
- Should publication default to push-only, draft PR, or local commit awaiting
  human review?
- Should direct work artifacts replace existing files, version files by
  work-item id, or write both a current view and an immutable snapshot?
