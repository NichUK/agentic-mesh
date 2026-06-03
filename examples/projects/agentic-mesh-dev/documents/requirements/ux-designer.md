# UX Designer Worklist

Status: sponsor-editable adoption output

## Role View

Agentic Mesh's first UI is not a web dashboard; it is the user's collaboration
surface. Teams messages, acknowledgements, Adaptive Cards, docs, and status
commands must make the system feel accountable and understandable. A future
control plane should amplify that, not hide the Git/config model.

## Outstanding Work

- Design Teams message patterns for directive received, work started, blocked,
  completed, handoff proposed, approval requested, and config reload result.
- Make acknowledgements visibly distinct from work instructions and ensure they
  do not contain misleading mention-like text.
- Design all-agents adoption status: roles targeted, roles started, roles
  completed, artifacts produced, blocked roles, and links.
- Design Adaptive Card patterns for approvals, yes/no, numeric, text,
  document, URL, and document-or-URL response types.
- Design CLI/status output for project health, role queues, connector cursors,
  hibernated instances, and recent errors.
- Design read-only control-plane preview: project list, role instances, queue
  depth, connector status, journal events, cost/usage summary, and artifact
  links.
- Design onboarding flow for self-managed small teams and enterprise assisted
  deployments.
- Add content guidelines for agent messages: concise confirmation first,
  work/evidence links next, no fake completion.

## Risks And Decisions

- Risk: Users see messages but cannot tell whether work is happening.
  Mitigation: every long-running instruction gets confirmation plus progress
  and completion signals.
- Risk: Dashboard becomes a second config authority. Mitigation: UI reads and
  edits Git-backed config with visible commits.
- Decision needed: first commercial UI scope: read-only operations dashboard
  versus setup wizard.

## Suggested Acceptance Criteria

- A sponsor can tell from Teams whether a directive was received, queued,
  started, completed, or blocked.
- A user can distinguish direct work from lifecycle handoff and approval.
- Control-plane UI concepts map directly to existing state/journal data.
