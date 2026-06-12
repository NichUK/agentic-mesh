# V2 Teams Connector Security Architecture

Status: downstream security architecture draft

Owner role: security-architect

Date: 2026-06-12

Source documents:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`
- `docs/architecture/authentication.md`

## Purpose

This document defines security, identity, privacy, retention, and audit
requirements for the v2 Teams connector.

The Teams connector is a human collaboration surface. It must not become the
runtime authority for orchestration, approvals, durable work, role-to-role
communication, or audit truth. The v2 runtime remains the authority for state,
safe-output calls, approvals, routing decisions, event journals, and delivery
records.

## Security Outcomes

- Teams and Entra permissions are least-privilege, tenant-admin reviewable, and
  bound to connector deployment profiles.
- Role identities are human-visible collaboration identities, not independent
  runtime authorities.
- Human authorization is explicit for sponsors, collaborators, operators, and
  project members.
- Direct messages remain private to the human and addressed role until explicit
  promotion.
- Project channels are treated as shared project context with clear membership
  and retention boundaries.
- Raw Teams content, compacted summaries, source-linked decisions, and audit
  evidence have distinct retention and classification rules.
- Every durable state change is attributable to an authorized actor and a
  runtime safe-output action.
- Tenant consent, app installation, and admin controls are documented before
  deployment.

## Identity Model

### Role Identities

Each configured role should be addressable in Teams as a specialist identity.
The product preference is separate role-agent identities backed by one
connector runtime.

Implementation may use separate Teams bot/app registrations, one Teams app with
role-addressable surfaces, or a hybrid. The security boundary is the runtime
role instance, not the visual Teams identity. Every inbound and outbound event
must resolve to:

- project id
- role template id
- role instance id, when a specific instance is assigned
- connector identity
- Teams tenant id
- Teams conversation, channel, thread, and message references
- human sender or recipient identity
- correlation id

Visual role identity must not imply a separate credential or authorization
scope unless the deployment profile explicitly assigns one. If separate app
registrations are used, each registration must have the same minimum permission
review, owner assignment, credential rotation policy, and emergency disablement
path.

### Human Identities

Teams users must map to runtime people records before they can perform
privileged actions. The mapping should include:

- tenant user id or Entra object id
- Teams user id, where different from Entra object id
- display name and user principal name for audit display
- project membership
- sponsor eligibility
- operator eligibility
- optional group-derived authorities

The connector must not rely on display names, email aliases, or free-text Teams
mentions as authorization evidence.

### Connector Service Identity

The connector service identity is a deployment concern under the authentication
model. Its credential material must be supplied through deployment secrets,
managed identity, workload identity, certificates, or OAuth bindings. Secret
values must never appear in project YAML, system repos, conversation events,
delivery records, logs, traces, prompt context, role memory, or generated
artifacts.

## Teams And Entra Permissions

The MVP should start with the narrowest permission set that supports configured
project teams, DMs, channel messages, mentions, proactive replies, delivery
status, and app installation checks.

### Permission Principles

- Prefer Teams bot activity context for messages the bot is directly installed
  into or addressed by, rather than broad tenant-wide message search.
- Avoid tenant-wide read permissions for all chats or all channel messages
  unless an enterprise deployment explicitly accepts that risk.
- Prefer resource-specific consent or scoped installation where Teams supports
  it for team or chat context.
- Separate setup-time/admin permissions from runtime message-processing
  permissions.
- Store only permission grants and consent metadata needed for audit; do not
  store tokens.
- Revalidate exact Microsoft Graph and Teams permission names during
  implementation, because the final implementation approach is still open.

### Candidate Runtime Capabilities

The connector may need permission to:

- receive bot activities in direct messages, group chats, and configured team
  channels where the app is installed
- send replies and proactive messages back to existing conversations
- read team, channel, chat, and member metadata for configured project bindings
- resolve mentioned users and role bot identities
- inspect app installation state for configured teams and chats
- record delivery success or failure with Teams message ids

If Graph application permissions are needed, they must be reviewed as high
impact. Candidate permission families include team/channel metadata, app
installation inspection, and message send or reply operations. Broad message
read permissions such as tenant-wide channel or chat reads require explicit
security approval and a documented reason why bot activity context is
insufficient.

### Setup And Administration Capabilities

Setup flows may require elevated permissions to install or validate the Teams
app, bind it to a project team, and grant resource-specific consent. These
permissions should be used only by an authorized operator or admin setup
process and should not be present in the normal message-processing runtime if
they can be separated.

## Authorization Model

### Sponsors

Sponsors are humans authorized to steer project work, answer sponsor questions,
approve gates, accept risks, and request durable work. Sponsor authority must
come from project configuration, mapped Entra groups, or an operator-approved
people record.

Sponsor actions accepted from Teams must be bound to the original question,
approval, work item, risk, or release gate. The runtime should reject or
request clarification for sponsor-like instructions from users who are not
authorized sponsors for the project.

### Project Humans

Project humans may ask questions, provide context, participate in channel
discussion, and be mentioned by agents. They may not approve gates, accept
risks, change connector configuration, or promote sensitive private content
unless project policy grants that authority.

### Operators

Operators can install, configure, disable, and rotate the connector. Operator
actions must be separated from sponsor actions. A project sponsor approving a
feature does not automatically have permission to grant tenant-wide Graph
permissions or change retention policy.

Operator activities must be audited with actor, time, project, tenant, config
version, and before/after values where applicable.

### Role Agents

Role agents may reply, ask humans for clarification, propose work, request
approval, summarize conversation, and promote context only through allowed
safe-output calls. Role agents may not grant themselves Teams permissions,
change project membership, bypass promotion policy, or use Teams as an
agent-to-agent transport.

## Direct Message Privacy

A direct message to a role is private to the human and addressed role by
default. It should be captured as a private conversation scope with access
limited to:

- the sender
- the addressed role instance or role service
- runtime operators with explicit break-glass or support authority
- security/audit reviewers under configured enterprise policy

No work item, shared channel summary, role memory entry, or document-library
update should be created merely because a direct message was sent.

Private DM content may become shared project knowledge only through explicit
promotion, such as:

- the human asks the role to turn the conversation into tracked work
- the role proposes durable work through a safe-output call and records the
  rationale
- the human approves promotion of a private decision or instruction
- policy allows promotion of non-sensitive operational facts and the role cites
  the source conversation

Promotion must record what was promoted, who or what initiated it, the target
artifact, the original source reference, the classification at time of
promotion, and any redactions applied.

## Project-Channel Shared Context

Configured project channels are shared project context. Humans who post in
those channels should expect the connector to capture messages as project
conversation events, subject to project membership, classification, and
retention policy.

Channel capture must still respect boundaries:

- Only configured project teams and channels are in scope.
- Feature or epic channels inherit the project boundary but may narrow context.
- Private Teams channels require explicit project binding and separate consent.
- Messages from channels outside the project binding must not be ingested.
- Membership changes should trigger a connector access review.

Unmentioned channel messages should not automatically wake every role. They may
be retained for future context, relevance checks, compaction, and durable
artifact creation when a safe-output call authorizes it.

## Retention And Compaction

Retention policy must distinguish raw Teams messages, private conversation
records, shared channel context, compacted summaries, durable documents, and
audit evidence.

Recommended defaults for MVP templates:

- Raw private DM messages: retain for 90 days by default, configurable per
  project, then delete or cryptographically shred raw body text after
  compaction if policy allows.
- Raw project-channel messages: retain for 180 days by default, configurable
  per project, then delete raw body text after compaction unless legal hold or
  audit policy requires longer retention.
- Compacted context summaries: retain for the life of the project plus one
  release/archive period, because they are project memory accelerators.
- Source-linked durable decisions, requirements, risks, approvals, and release
  evidence: retain according to document-library and audit policy, not Teams
  raw-message policy.
- Connector delivery records and idempotency keys: retain long enough to
  support incident investigation, replay safety, and release audit evidence;
  recommended minimum is one year for enterprise profiles.

Compaction must preserve important decisions, requirements, risks,
constraints, approvals, instructions, and source references. Compaction must not
launder private DMs into shared context. Summaries derived from private DMs
must remain private unless promotion was explicitly recorded.

Retention settings must be project-configurable, visible to operators, and
included in release evidence for deployments that enable the connector.

## Audit Evidence

The connector must emit audit evidence for:

- app registration, installation, consent, and permission grants
- connector configuration changes
- role identity and alias mappings
- human identity mapping and authorization decisions
- inbound Teams message ingestion with source references
- direct-message visibility scope
- project-channel context capture
- promotion of private conversation into work or durable knowledge
- safe-output calls caused by Teams conversation
- relevance checks for team-wide prompts, including no-op decisions
- sponsor questions, answers, approvals, and risk acceptances
- outbound delivery attempts, retries, failures, and final Teams message ids
- retention, compaction, deletion, and legal-hold actions
- break-glass access to private conversation content

Audit records should include actor, actor type, project id, role instance id
when applicable, tenant id, source event id, correlation id, decision, reason,
timestamp, policy version, and resulting artifact links. Audit evidence may
store Teams message references and hashes after raw body retention expires.

## Data Classification

The connector should support at least these classification levels:

- Public: safe for open project documentation.
- Internal: normal project conversation and delivery coordination.
- Confidential: sponsor direction, private DMs, security-sensitive design,
  customer context, financial information, or roadmap-sensitive material.
- Restricted: secrets, credentials, regulated personal data, legal privilege,
  incident material, or data subject to special handling.

Default classification:

- Direct messages default to Confidential.
- Project-channel messages default to Internal unless the project, channel, or
  message marker sets a higher classification.
- Private Teams channels default to Confidential.
- Connector credentials and tokens are Restricted and must not be ingested as
  conversation content.
- Durable artifacts inherit the highest classification of promoted source
  material unless a human with authority downgrades it with justification.

Restricted material must not be added to prompt context unless a policy and
tool boundary explicitly permit that use. Secret-like content observed in Teams
must be redacted, flagged, and excluded from role memory and document-library
summaries unless a security process decides otherwise.

## Consent And Admin Installation

Enterprise tenants need an explicit consent and installation package before
enabling the connector. The package should include:

- implementation approach for role identities
- requested Teams and Entra permissions
- whether permissions are delegated, application, or resource-specific
- admin consent requirements
- app ownership and credential rotation responsibilities
- project team and channel bindings
- private channel handling
- data captured from DMs and channels
- retention defaults and project overrides
- audit evidence emitted
- disablement and rollback process
- support/break-glass policy

The connector must fail closed when installation, consent, or team binding is
missing. It should record a visible blocker rather than silently dropping
messages or accepting messages without a valid project authorization boundary.

## Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Separate role identities increase app registration and permission sprawl. | Prefer one connector runtime, shared permission review, standardized app owner metadata, automated install validation, and emergency disablement for all role identities. |
| Broad Graph permissions expose more Teams data than the project needs. | Prefer bot activity context, resource-specific consent, configured team/channel bindings, and explicit security approval for any tenant-wide read. |
| Private DMs leak into shared project context. | Enforce private conversation scope, require explicit promotion records, keep private compaction private, and audit every promotion. |
| Channel capture surprises humans. | Publish channel binding and retention notices, limit capture to configured channels, and make project-channel context rules visible during onboarding. |
| Unauthorized humans approve gates or accept risk from Teams. | Resolve users to runtime people records and enforce sponsor/operator authorization before accepting privileged safe-output actions. |
| Role agents use Teams as agent-to-agent transport. | Prompt roles to use runtime consult/handoff tools, reject bot-to-bot routing as orchestration input, and audit attempted misuse. |
| Relevance checks silence important specialist input. | Record relevance scores and justifications, allow justified below-threshold responses, and include QA scenarios for missed-response risk. |
| Retention compaction loses important decisions. | Require source-linked summaries, preserve decisions in the document library, and test compaction before raw body deletion. |
| Connector credentials leak into logs or prompt context. | Resolve credentials through auth bindings, redact secrets from telemetry, and block token/API-key patterns in conversation ingestion. |
| Misconfigured project channel crosses confidentiality boundaries. | Require explicit project-channel binding, validate team membership, review private channel configuration, and provide a disablement path. |

## Security Acceptance Criteria

- Given a Teams app is installed for a project, when an operator reviews the
  connector setup, then the requested permissions, consent type, app owners,
  credential rotation path, channel bindings, and retention settings are
  visible.
- Given a human sends a direct message to a role, when the connector stores the
  message, then the conversation scope is private to that human and role and no
  work item or shared artifact is created by default.
- Given private DM content is promoted into tracked work, when the promotion is
  recorded, then the audit trail identifies the source message, initiating
  actor or role, target artifact, classification, and redaction status.
- Given a user replies to an approval request from Teams, when the runtime
  processes the reply, then the user must resolve to an authorized sponsor for
  that project before the approval is accepted.
- Given a user tries to change connector configuration from Teams, when the
  request is processed, then operator authorization is required and the change
  is rejected or routed to an operator workflow if the user lacks authority.
- Given an unmentioned project-channel message is received, when it is stored,
  then it becomes shared project context without waking all roles or creating
  durable work by default.
- Given a Teams message is outside configured project teams or channels, when
  the connector receives or discovers it, then the message is not ingested into
  project context.
- Given a team-wide trigger is used, when roles perform relevance checks, then
  relevance decisions, scores or labels, and response/no-op outcomes are
  recorded for audit.
- Given raw Teams message retention expires, when compaction or deletion runs,
  then source-linked decisions, requirements, risks, approvals, and release
  evidence remain available in durable project artifacts.
- Given a connector send fails, when retry handling completes or exhausts, then
  the runtime records delivery status, failure reason, retry count, and next
  action without leaking credentials.
- Given a private or restricted message contains a secret-like value, when
  ingestion detects it, then the value is redacted from logs, traces, prompt
  context, summaries, and role memory, and a security event is recorded.
- Given tenant consent or app installation is missing, when connector startup or
  message processing runs, then the connector fails closed and records a
  visible blocker.

## Open Security Decisions

- Confirm whether MVP role identities use separate app registrations, one app
  with role-addressable surfaces, or a hybrid.
- Confirm final Microsoft Graph and Teams permission names after the
  implementation approach is selected.
- Confirm whether resource-specific consent is available for every required MVP
  action in the target tenant profile.
- Confirm enterprise retention defaults with sponsor and compliance input.
- Confirm whether raw body deletion should use physical deletion,
  cryptographic shredding, or backend lifecycle policy per storage adapter.
- Confirm break-glass review workflow for support access to private DMs.

## Review Log

- RL-001 | security-architect | downstream-security | full document | Created
  initial Teams connector security architecture covering permissions, role and
  human identity, authorization, DM privacy, project-channel context,
  retention, audit, data classification, consent, risks, mitigations, and
  security acceptance criteria. | incorporated 2026-06-12
