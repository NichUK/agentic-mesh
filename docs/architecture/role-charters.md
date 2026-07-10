# Role Charters And Standards

Status: active V4 guidance

Date: 2026-06-04

## Purpose

Agentic Mesh role templates need more than a short purpose line. The `purpose`
field stays brief, while the role charter explains how the agent should act as
a specialist in a project mesh.

Role charters are not theatrical personas. They are operating models: what the
role owns, where it advises, where it must not overreach, how it collaborates,
what quality means, and what it should remember.

## Charter Fields

Role templates may declare:

- `role_profile`: professional stance and operating model
- `accountabilities`: durable responsibilities owned by the role
- `decision_rights`: decisions the role owns, advises on, or escalates
- `boundaries`: areas the role must not take over
- `collaboration_style`: how the role reviews, pushes back, consults, and hands off
- `quality_bar`: completion standards before marking work done
- `memory_focus`: facts and lessons worth retaining in role memory
- `core_workflows`: repeatable role workflows with triggers, inputs, outputs, and artifacts
- `standards_references`: frameworks that informed the role
- `anti_patterns`: common poor behaviours to avoid

V4 materialisation renders these fields explicitly into each role instance's
external `AGENTS.md`. Project instructions, document accountabilities, and the
resolved flow/RACI are rendered separately so template and project authority
remain distinguishable.

## Standards Use

Agentic Mesh adapts established standards as design input rather than copying
their text or enforcing a single process.

- BMAD informs specialist agent roles, named workflows, artifact-driven handoffs, and role-specific capabilities.
- Scrum informs accountability boundaries, product ownership, developer self-management, and flow transparency.
- SFIA informs concise skill profiles and responsibility focus; roles should not become encyclopedic.
- BABOK informs business analysis framing, elicitation, stakeholder context, requirements lifecycle thinking, and evidence separation.
- TOGAF informs enterprise architecture governance, capability fit, architecture content, and decision traceability.
- ISTQB and BDD practice inform quality planning, test evidence, defect classification, and behaviour scenarios.
- OWASP SAMM and NIST SSDF inform secure SDLC responsibilities, control evidence, and security review gates.
- Prompt engineering practice informs prompt contract design, tool-use
  guidance, behavioural regression scenarios, and diagnosis of invalid or
  low-value agent outputs.

Projects can override role instructions, tools, flows, gates, and document
accountabilities. The stock charters are useful defaults, not a rigid operating
model.

## Enterprise Architect

Enterprise Architect is the accountable steward of durable enterprise
architecture, not merely a reviewer for one lifecycle stage. The charter owns:

- architecture vision, principles, strategy, standards, and reference building blocks
- baseline, target, and transitional states across Business, Data, Application, and Technology
- business capability maps, value streams, organisation/process views, and target operating model
- architecture requirements, gaps, dependencies, transition architectures, and roadmap
- conformance findings, exceptions, implementation governance, and architecture change reconciliation

Solution Architect remains accountable for solution-level design. Business
Analyst owns business analysis, Product Manager owns product value and scope,
Security Architect owns security architecture decisions, and Platform Engineer
owns platform implementation evidence. These roles contribute evidence to the
Enterprise Architect's portfolio without transferring their specialist decision
rights.

Every Product Definition records `architecture_impact` as `none`, `material`,
or `uncertain`. Material and uncertain changes route to Enterprise Architect;
`none` may take the shorter path only with a recorded rationale. Strategic
target-state or operating-model changes, funded roadmap commitments, and
high-impact exceptions require sponsor authority.

## Workflow Pattern

`core_workflows` deliberately resemble BMAD-style agent workflows:

```yaml
core_workflows:
  - workflow_id: define-product-slice
    trigger: Business framing is ready for product definition.
    inputs:
      - Business brief
      - Sponsor constraints
    outputs:
      - Product story
      - Acceptance criteria
    artifacts:
      - docs/product/stories.md
```

The workflow tells the agent what kind of work it performs and what evidence it
should leave behind. It does not replace the project flow; the flow still owns
state transitions, gates, consult routes, and handoffs.

## Prompt Engineer Role

The `prompt-engineer` role is a specialist SDLC role for agent-facing work. It
does not own product scope, architecture, implementation, QA, or release
decisions. It owns the quality of prompt contracts and advises other roles when
agent behaviour, safe-output tools, context loading, role memory, or prompt
regression evidence affects the work.

Prompt Engineer and Project Manager are peers, not parent/child roles. Project
Manager owns project control, stale-work sweeps, governance hygiene, and
escalation; Prompt Engineer owns specialist diagnosis and design for prompts
and agent behaviour. Project Manager may consult Prompt Engineer when a delivery
blocker appears to be caused by prompt wording, missing context, tool-contract
confusion, or invalid agent output, but the consultation does not give Project
Manager authority over prompt-design decisions or give Prompt Engineer
authority over project sequencing.

Use this role when a slice changes:

- role prompts or prompt components
- safe-output tool instructions
- conversational behaviour
- role handoff or blocker wording
- memory/context-loading instructions
- agent failure recovery guidance
- behavioural tests for prompt-driven work

The role should work from evidence: generated prompts, safe-output traces,
worker results, runtime interpretation, sponsor feedback, and role-owned
documents.

## Decision Rights

Decision rights prevent role confusion. A role may:

- `owns`: make the decision or declare readiness within its accountability
- `advises`: provide specialist input to another accountable role
- `escalates`: call out unresolved decisions, risks, or missing authority

The lifecycle machinery should route and record work. It should not make the
specialist decision on behalf of the role.

## Role Memory

`memory_focus` guides what belongs in role memory. Documents remain canonical.
Role memory is a concise, source-linked accelerator that captures recurring
decisions, stakeholder preferences, known risks, and lessons learned.

If memory disagrees with the document library, the agent must trust the
document library and refresh memory.
