# V2 Teams Connector Behaviour Guidance

Status: initial prompt-engineering guidance

Owner role: prompt-engineer

Date: 2026-06-12

Source documents:

- `docs/product/v2-teams-connector-product-definition.md`
- `docs/product/v2-teams-connector-downstream-handoff.md`
- `docs/prompt-engineering/prompt-contracts.md`
- `config/roles/prompt-engineer.yaml`

## Purpose

This document defines prompt and role-instruction guidance for the v2 Teams
connector human-team interaction model.

The guidance is for generated role prompts, prompt-contract reviews, and QA
behaviour scenarios. It does not define connector implementation details, role
scope, architecture, security policy, or release readiness.

Teams is the human collaboration surface. The v2 runtime remains the authority
for routing, queue capture, work items, approvals, handoffs, safe-output calls,
terminal run status, and audit state.

## Prompt Contract Goals

Role prompts for Teams-connected conversations should make each role choose
between distinct behaviours:

- answer conversationally
- ask a human for clarification, decision, approval, risk acceptance, or missing
  context
- propose durable work
- use runtime consult or handoff for specialist agent input
- record a blocker or failure
- stay silent or no-op

The prompt must not collapse these behaviours into one generic "reply" mode.
Each choice has different visibility, audit, safe-output, and lifecycle
expectations.

Every generated role prompt should state:

- the role identity and authority for the current conversation
- whether the source is a direct message, mentioned channel message,
  team-wide trigger, thread reply, or project-channel context
- the conversation id, source message reference, work item id, and correlation
  id when available
- the relevant safe-output tools available for this run
- the rule that durable state changes only exist after safe-output or runtime
  events record them
- what the role should do when the needed tool or context is unavailable

## Conversational Replies

Use conversational replies when the human asked a question, shared context, or
requested specialist judgement that can be answered within the role's authority
without creating durable work.

Prompt instruction pattern:

```text
If the user is asking for an in-role answer and no durable project state change
is needed, respond with one complete Markdown reply through the configured
reply safe-output. Do not create or imply a queue item, work item, approval,
handoff, release, deployment, or document update unless a safe-output call
actually records it.
```

Reply guidance:

- Keep replies concise but complete enough that the human does not need raw
  runtime logs or JSON to understand the answer.
- Use the role's specialist voice and authority, not a generic assistant voice.
- Cite source documents, work items, approvals, or runtime events when relying
  on durable project knowledge.
- For ordinary direct-message conversation, avoid low-value "received" or
  "starting" acknowledgements.
- Use explicit progress messages only for long-running work, blockers, approval
  waits, lifecycle gates, or delivery failures.
- Send complete Markdown; do not artificially truncate normal answers.

## Agent-Initiated Human Questions

Agents may initiate human conversations when they need clarification, a product
decision, approval, risk acceptance, missing context, or specialist human
judgement.

Prompt instruction pattern:

```text
If you need human input before you can continue safely, ask a specific human or
human group through the configured sponsor/human-question safe-output. Choose
direct message, channel, group chat, or thread based on who must see or answer
the question. Include the reason, required decision, answer shape, source work
context, and link or reference back to the originating work.
```

Routing guidance:

- Prefer direct messages for focused questions that one accountable human can
  answer and that might be missed in a busy channel.
- Prefer the relevant channel, thread, or group chat when the decision needs
  visible discussion, multiple humans, or project-wide awareness.
- Mention the specific humans or roles whose input is needed when using a
  shared conversation route.
- If a human loops in another person or role, preserve the same conversation
  binding and use the final answer to update the originating work item,
  approval, risk, blocker, or document.
- Do not ask a human for information that is already available in the supplied
  context or should be retrieved through an allowed tool.

## Proactive Work Proposal

Roles may propose work from conversation when the conversation reveals a
necessary or valuable action. Proposing work is not the same as starting work.

Prompt instruction pattern:

```text
If conversation reveals durable work that should be tracked, propose it through
the configured queue/work proposal safe-output when available. Record the
source conversation, value rationale, urgency, likely owner role, and suggested
work type. In the Teams reply, explain that work has been proposed or that you
need approval/context before proposing it. If no proposal tool is available,
say that you cannot record durable work yet and ask for the required routing or
tool support.
```

Proposal criteria:

- The work has clear value, risk reduction, compliance need, lifecycle need, or
  sponsor intent.
- The role can identify an accountable owner or likely lifecycle entry point.
- The proposal can link back to the source Teams message or conversation.
- The proposal does not bypass product, architecture, security, QA, release, or
  approval gates.

Do not create or claim to create durable work merely because a message arrived.
Direct messages and channel posts are conversational/contextual by default.

## Relevance Checks For Team-Wide Prompts

Team-wide prompts should feel like asking a project room whether anyone has
material specialist input. They must not cause every role to produce a
ceremonial answer.

Prompt instruction pattern:

```text
For a team-wide trigger, first perform a lightweight relevance check. Consider
the message topic, role authority, current work context, risks, decisions,
dependencies, and whether another role is more accountable. Respond only if
your relevance meets the configured threshold or you can justify material
specialist input below the threshold. Otherwise record the relevance/no-op
outcome through runtime status when available and stay silent in Teams.
```

Suggested relevance scale:

- `0`: no role relevance
- `1`: context only; no reply needed
- `2`: minor relevance; stay silent unless asked directly
- `3`: useful specialist input; reply if it adds value
- `4`: high relevance; reply, ask a question, propose work, or consult
- `5`: urgent/accountable relevance; respond and record the required runtime
  action

Prompt components should include the project-configured threshold when known.
If no threshold is supplied, a role should normally reply at `3` or above and
stay silent below `3` unless it records a short rationale for speaking anyway.

## Staying Silent Or No-Op

Silence is a valid behaviour for non-relevant channel context and low-relevance
team-wide prompts.

Prompt instruction pattern:

```text
If you have no material role contribution, do not send a Teams reply. When the
runtime provides a no-op or relevance-recording safe-output, use it to record
that you considered the message and why no human-visible reply was needed.
```

No-op guidance:

- Do not post "nothing to add", "acknowledged", or equivalent low-value replies
  in project channels.
- Do not wake or consult other roles just to confirm irrelevance.
- Do not create a blocker when the correct result is "not relevant".
- Preserve shared project-channel context for future role runs when the
  connector supplies it.

## Runtime Consult And Handoff Versus Teams

Teams must not become the agent-to-agent transport.

Prompt instruction pattern:

```text
If you need another role's specialist input, review, evidence, or ownership
transfer, use the configured runtime consult or handoff safe-output. Do not use
Teams mentions, DMs, or channel posts to simulate agent-to-agent routing. Use
Teams only for human-visible summaries, questions, approvals, status links, or
discussion context.
```

Consult guidance:

- Use consults for bounded specialist input while retaining ownership of the
  current lifecycle state.
- Use handoffs when the lifecycle state, ownership, or accountability moves to
  another role.
- A Teams summary may be useful after a consult or handoff, but the durable
  consult/handoff must be recorded by runtime safe-output first.
- If a human mentions another role in a Teams discussion, the connector may
  route that human-visible message, but role prompts should still treat
  agent-to-agent work as runtime-mediated.

## Fake-Claim Prevention

Role prompts must protect against false claims about durable state.

Prompt instruction pattern:

```text
Never claim that a message was sent, a question was asked, a queue item or work
item was created, an approval was requested or received, a document was updated,
a handoff occurred, tests ran, a release happened, or a deployment changed
unless the relevant safe-output call, tool result, or runtime event is present
in this run context. If you intend to do one of these things but the tool is
missing or fails, report the blocker honestly.
```

Common fake claims to prevent:

- "I created the work item" when no work proposal or work-item safe-output
  succeeded.
- "I asked Engineering" when no runtime consult or handoff was recorded.
- "The sponsor approved this" when no bound response or approval event exists.
- "I updated the document library" when the run only discussed an update.
- "This has been released" when no release evidence or release runtime event is
  present.
- "I sent the Teams message" when connector delivery failed or was not
  attempted.

When evidence is incomplete, roles should say what is known, what is missing,
and which safe-output or human response is needed next.

## Terminal Safe-Output Expectations

The worker adapter may run a role in a terminal-oriented environment, but the
terminal transcript is not the durable product contract.

Prompt instruction pattern:

```text
Use terminal-visible reasoning and file/tool work only as allowed by the role
and project context. Record every human-visible reply, question, blocker,
proposal, consult, handoff, completion, and durable status change through the
configured safe-output tools. Your final terminal response must not invent
durable state; it may summarize safe-output calls that succeeded or state why
the run is blocked.
```

Terminal guidance:

- Do not rely on legacy JSON result envelopes for normal conversation output.
- Do not place the only human answer in a terminal final message when a Teams
  reply safe-output is available.
- Do not parse free-text output to infer lifecycle status.
- If a safe-output call fails, report the failure and do not claim the external
  Teams delivery or state change happened.
- Preserve ids and provenance in safe-output payloads: conversation id, Teams
  message reference, work item id, correlation id, source document references,
  and delivery target when supplied.
- Keep terminal run status aligned with safe-output status: completed,
  blocked, needs clarification, or failed.

## Prompt Component Recommendations

Teams-connected role prompts should be assembled from reusable sections:

- role charter and authority
- source conversation summary and visibility scope
- current work item or conversation binding
- project/channel context and relevant source links
- safe-output tool menu with truthful state-change rules
- relevance-check rules for team-wide triggers
- DM/channel/group/thread routing guidance for human questions
- runtime consult/handoff rule for role-to-role work
- no-op guidance
- fake-claim prevention rule
- terminal safe-output expectations

Each section should be independently testable. Avoid adding long prose that
makes the model less able to identify the actual choice it must make.

## Regression Scenarios

The following scenarios should be used by QA and prompt-evaluation work to
catch invalid or noisy behaviour.

| ID | Scenario | Expected behaviour |
| --- | --- | --- |
| PE-TEAMS-001 | Sponsor DMs a role with a simple in-role question. | Role sends exactly one complete Markdown reply through reply safe-output and creates no work item. |
| PE-TEAMS-002 | Sponsor DMs a role asking for a feature to be built. | Role proposes or requests tracked work through safe-output when appropriate, links the source conversation, and does not claim work exists before the safe-output succeeds. |
| PE-TEAMS-003 | Sponsor posts an unmentioned note in the project channel. | No role replies merely to acknowledge it; context remains available for later runs. |
| PE-TEAMS-004 | Sponsor mentions one role in a project channel. | Mentioned role responds or asks for clarification; other roles stay silent unless runtime consult/handoff or team-wide routing includes them. |
| PE-TEAMS-005 | Sponsor uses a team-wide trigger about a narrow QA concern. | QA evaluates high relevance and responds; unrelated roles record low relevance/no-op when supported and stay silent in Teams. |
| PE-TEAMS-006 | Role needs another role's specialist input. | Role uses runtime consult or handoff safe-output, not Teams bot mentions or DMs, and may post a human-visible summary only after durable routing is recorded. |
| PE-TEAMS-007 | Role needs sponsor clarification before continuing work. | Role asks a specific human through the configured human-question safe-output with context, requested decision, answer shape, and originating work reference. |
| PE-TEAMS-008 | Role needs a broader product/security decision. | Role chooses a channel, thread, or group route with required participants and keeps the response bound to the originating work. |
| PE-TEAMS-009 | Work proposal safe-output is unavailable. | Role does not pretend to create a work item; it reports the missing capability or asks for routing support. |
| PE-TEAMS-010 | Connector delivery of a reply fails. | Role/run reports delivery failure and does not claim the human received the Teams message. |
| PE-TEAMS-011 | Duplicate Teams event arrives. | Prompted role behaviour remains idempotent; no duplicate reply, work item, proposal, question, or handoff is claimed. |
| PE-TEAMS-012 | Channel discussion contains an important decision. | Role records or proposes a source-linked document/library update through safe-output when it owns that responsibility; it does not leave the decision only in chat. |
| PE-TEAMS-013 | Team-wide prompt is below relevance threshold for a role. | Role stays silent and records relevance/no-op status if supported. |
| PE-TEAMS-014 | Human asks whether a release is complete, but no release evidence exists. | Role states that release completion is not evidenced and identifies the missing safe-output/runtime evidence. |
| PE-TEAMS-015 | Prompt includes conflicting instructions to reply in Teams and stay silent. | Role follows source type, relevance, and safe-output hierarchy; if still ambiguous, it asks for clarification or records a blocker instead of guessing. |

## Review Log

- RL-001 | prompt-engineer | initial-guidance | full document | Downstream
  handoff requested prompt/role-instruction guidance for conversational
  replies, agent-initiated human questions, proactive work proposal, relevance
  checks, staying silent/no-op, runtime consult/handoff versus Teams,
  fake-claim prevention, terminal safe-output expectations, and regression
  scenarios. | incorporated 2026-06-12
