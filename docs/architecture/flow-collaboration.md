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
