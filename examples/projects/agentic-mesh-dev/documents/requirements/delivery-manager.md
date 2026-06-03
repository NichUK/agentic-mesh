# Delivery Manager Worklist

Status: sponsor-editable adoption output

## Role View

The project has good architecture direction and several implementation slices,
but delivery needs a tighter backlog, branch discipline, definition of done,
and release cadence. The adoption process itself exposed an important delivery
gap: acknowledgements, work creation, execution, evidence, and completion must
be visibly distinct.

## Outstanding Work

- Convert design themes into a prioritized backlog with slices, dependencies,
  risks, and acceptance criteria.
- Track the current all-agents adoption incident as a bug and recovery slice:
  "all-agents acknowledgement echo must not become work".
- Establish `develop` as the daily integration branch, feature branches for
  slices, PRs back to `develop`, Copilot review, and periodic tested promotion
  to `main`.
- Add issue/PR templates covering product intent, test evidence, operational
  impact, and docs updated.
- Define release trains: dogfood baseline, OSS preview, enterprise preview.
- Create a smoke-test checklist for local Compose, Teams ingress, all-agents
  direct work, role queue processing, acknowledgements, and no auto-handoff.
- Add a decision gate for when runtime-generated docs are accepted as product
  evidence versus considered blocked, incomplete, or needing sponsor review.
- Maintain a public roadmap with near-term OSS hardening, Teams connector,
  worker adapter, control-plane, and deployment targets.

## Near-Term Slice Candidates

- Fix all-agents mention detection and connector echo suppression.
- Keep real worker execution visible in delivery status, including blocked
  runs caused by missing worker credentials or unavailable provider adapters.
- Add project adoption command/test that creates one work item per role and
  verifies expected artifacts.
- Implement config reload signal for listener/ingress containers.
- Add Compose generator boundary tests and backlog Terraform/Helm outputs.

## Risks And Decisions

- Risk: Dogfood runtime blocks when worker credentials are missing.
  Mitigation: surface the exact missing secret or mount in the role artifact
  and operational status.
- Risk: Local VM changes drift from Git. Mitigation: deploy only from commits
  or tagged worktrees and record commit id in runtime status.
- Decision needed: whether every sponsor directive requires a work item record
  visible in status before agent execution begins.

## Suggested Acceptance Criteria

- Every active slice has an owner role, status, acceptance criteria, and test
  evidence.
- `main` receives only tested releases from `develop`.
- The all-agents adoption flow produces 13 meaningful docs and zero handoffs
  or approvals unless explicitly requested.
