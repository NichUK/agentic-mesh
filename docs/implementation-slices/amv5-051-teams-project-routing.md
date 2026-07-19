# AMV5-051 — Teams project routing and ambiguous DMs

## Outcome

Resolve a Teams activity to a project and logical role only from configured,
active authority. When an authorized personal conversation can refer to more
than one project, return an explicit question instead of choosing a project.

## Scope and boundaries

- Channel routing requires one exact active tenant, Team, channel, and
  recipient bot application match.
- Personal-message routing requires the tenant and recipient bot application,
  then filters candidate projects through the authenticated sender's project
  authorization.
- A single authorized personal-message candidate routes directly. Multiple
  candidates return a structured `clarification_required` decision containing
  the exact candidate projects and a user-facing question.
- A subsequent choice is accepted only as a structured `selected_project_id`
  that exactly matches an authorized candidate. Message text and arbitrary
  project hints are never parsed for project selection.
- Unknown, duplicated, unauthorized, malformed, spoofed, or concurrently
  changed authority fails closed without exposing unrelated project details.
- The resolver verifies the selected active manifest digest and role binding
  immediately before returning a route.
- AMV5-052 owns Adaptive Cards and progress updates. This slice returns the
  connector action that an ingress adapter must deliver; it does not introduce
  a second Teams transport or card implementation.

## V4 reuse disposition

- Reuse V4's proven precedence of recipient bot identity over words appearing
  in message text.
- Rewrite project resolution against all active V5 manifest snapshots and
  authenticated project authorization.
- Reject V4's default-to-project-manager fallback, role-name parsing, and
  single-project configuration assumption.

## Acceptance criteria

- An authenticated channel activity routes only when tenant, Team, channel,
  recipient bot, active manifest, and role binding all agree.
- An authenticated personal activity with one candidate routes to it; an
  activity with multiple candidates has no selected project or role and asks
  the user to choose from only authorized candidates.
- An exact structured choice resumes the matching candidate; an invalid choice
  is rejected.
- Foreign tenants, Teams, channels, bots, senders, unknown projects, duplicate
  channel claims, malformed values, and activation races cannot produce a
  route.
- No message-text content can influence project or role selection.
- Generic runtime routing remains independent of Teams-specific code.
