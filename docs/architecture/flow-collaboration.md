# Flow Collaboration

Status: draft design

Date: 2026-06-03

## Intent

Agentic Mesh flows must support real work conversations, not just a fixed
left-to-right lifecycle pipeline.

A sponsor may ask any role-agent a question or ask for work that sits inside
that role's accountability. That agent may need to consult another role that
would normally appear earlier, later, or sideways in the lifecycle before it
can answer or complete the work. The flow definition therefore needs both
forward handoffs and governed consult routes.

## Forward Handoffs

`handoffs` move a work item from one lifecycle state to another after the
current state's exit criteria are met.

Forward handoffs represent lifecycle progression. They usually write the next
message to the owner role for the next state.

Example:

```yaml
handoffs:
  completed:
    target_state: quality_review
    target_role: qa-engineer
    message_type: sdlc.quality_review
```

## Consult Routes

`consults` let the current owner ask another role for bounded input without
completing or abandoning the current lifecycle state.

Consult routes may point backwards, forwards, or sideways in the lifecycle
graph. They are explicit so each role can know whom it is allowed to ask and
why.

Example:

```yaml
consults:
  product_scope:
    target_state: product_definition
    target_role: product-manager
    message_type: sdlc.consult.product_definition
    purpose: Clarify acceptance criteria, scope, priority, or user-visible behaviour.
```

Consults should preserve the same `work_item_id`, `work_item_type`, and
`correlation_id` unless the consult uncovers a genuinely separate piece of work
that needs a new tracked item.

Consult responses should update the relevant accountable document or evidence
record, then return context to the requesting role. A consult does not replace
required gates or forward handoffs.

## Planning And Review Loops

Software build work should be planned before execution. The SDLC flow includes
`implementation_planning` and `quality_planning` states before
`implementation`.

Plan review gates are loop points, not central-controller decisions. Affected
roles review the plan, write visible `## Review Log` comments in the relevant
document, and either approve, approve with comments, request concrete changes,
or identify a required sub-slice.

When reviewers disagree, agents should handle the loop like human SDLC peers:

- cite the specific document section or requirement in dispute
- propose a concrete change or acceptance condition
- attempt the configured number of written resolution loops
- request mediation only when the same disagreement remains unresolved
- keep the accountable role responsible for the specialist decision

The lifecycle machinery records and routes the loop; it does not decide who is
right.

## Sub-Slices

A sub-slice is a first-class work item linked to a parent work item. It is used
when a plan review or implementation review uncovers a smaller piece of work
that should be handled by a focused set of roles.

Sub-slices still use the normal flow, gates, reviews, and evidence capture. The
flow can start near the affected area, but unexpected impacts must still be
able to route through consults or handoffs.

## Cross-Mesh Handoffs

Projects can declare peer meshes such as SDLC, Sales, Marketing, Support,
Governance, or Enterprise Architecture. Roles may be shared between meshes.

A cross-mesh handoff creates a new linked slice in the receiving mesh. The
receiving mesh owns its own flow, review gates, and documentation outputs while
the originating work item keeps a reference to the spawned work.

## Sponsor-Initiated Work

When a sponsor asks an agent to do something, or asks a question that requires
investigation, implementation, decision support, or cross-role input, the agent
must not treat it as invisible side work.

The contacted agent must create or request a tracked work item. The project
flow defines how this works:

```yaml
sponsor_initiated_work:
  allow_from_any_state: true
  default_work_item_type: spike
  default_intake_state: business_analysis
  capture_rule: ...
  routing_rule: ...
  completion_rule: ...
```

If the work clearly fits the contacted role's accountability, the agent can
start it in that lifecycle state. If sponsor intent, scope, value, priority, or
risk is unclear, the agent should route it to the default intake state for
business framing.

The work then progresses through permitted consult routes, forward handoffs,
gates, evidence capture, and release or closure records until it is complete.

## Receipt Acknowledgement

When a human or another agent gives an instruction, the receiving role should
acknowledge receipt and confirm the interpreted intent before or as work
begins. This is especially important for long-running work, because humans need
to know whether the mesh heard the instruction.

For collaboration connectors, inbound human messages should be turned into
tracked work or an explicit blocker. They should not be accepted silently and
then dropped.

## Design Consequences

- Flows are collaboration graphs, not simple ordered lists.
- Role templates remain stable; project flows decide which consults and
  handoffs are allowed.
- Parallel slices, features, spikes, defects, and research tasks keep separate
  work item identity.
- Direct sponsor conversations can start work anywhere, but they still become
  tracked work.
- Consult routes are auditable lifecycle events and should be visible in
  traces, logs, Teams messages, and document contribution history.
