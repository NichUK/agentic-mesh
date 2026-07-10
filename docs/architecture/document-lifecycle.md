# Document Lifecycle And Ownership

Status: draft design

Date: 2026-06-02

## Purpose

Agentic Mesh roles need durable accountability for project documents without
making one role the only possible author.

Document ownership means stewardship:

- ensure the document exists
- ensure required sections are populated
- check correctness, freshness, and consistency
- reconcile contributions from other roles
- edit or request corrections where the project permits
- approve the document for the current lifecycle state

Ownership does not mean exclusive authorship. Other roles can contribute
sections, evidence, diagrams, findings, test results, release notes, and
implementation details.

## Project Configuration

Projects declare document accountabilities:

```yaml
document_accountabilities:
  docs/product/stories.md:
    owner_role: product-manager
    accountability: accountable_owner
    can_edit_contributions: true
    review_on_contribution: true
    required_sections:
      - problem
      - acceptance_criteria
    contributing_roles:
      - business-analyst
      - ux-designer
      - solution-architect
    lifecycle_events:
      - document.contribution_added
      - document.owner_review_requested
      - document.owner_review_completed
```

## Lifecycle Events

When any role writes to an accountable document, the artifact store should emit:

- `document.contribution_added`

If `review_on_contribution` is true and the contributor is not the owner, it
should also emit:

- `document.owner_review_requested`

The owner then reviews the contribution and emits:

- `document.owner_review_completed`

The review result should include:

- work item id
- document path
- owner role
- contributor role
- reviewed sections
- status: `approved`, `changes_requested`, or `blocked`
- notes

## Gates

Flow states can declare gates:

```yaml
gates:
  - gate_id: product_story_owner_review
    type: document_owner_review
    required_documents:
      - docs/product/stories.md
    required_review_status: approved
    reviewer_role: product-manager
```

A gate should block handoff until required documents exist and required owner
reviews are complete for the current work item.

Gate evaluation should consume lifecycle events and state records, not infer
readiness only by scanning file contents.

## Proportional Enterprise Architecture Governance

The stock SDLC flow is TOGAF-aligned without requiring the full ADM for every
work item:

1. Product Definition records `architecture_impact`, rationale, affected
   domains, reviewer, and any decision reference.
2. `none` takes the shorter route after the rationale is recorded.
3. `material` or `uncertain` enters `enterprise_alignment` and produces
   `040-enterprise-alignment.md`, including durable portfolio impacts and
   conformance requirements.
4. Enterprise Architect reviews the solution design before implementation
   planning. An approved conformance result or sponsor-approved exception is
   required for downstream implementation and release.
5. Delivery readiness and release reference the conformance disposition.
   Release informs Enterprise Architect so baseline/target views and the
   architecture change log can be reconciled.

Structured conditions use only allow-listed `when.field` and `when.in` values.
Arbitrary expressions are not executable configuration. Architecture,
security, implementation, test, and release evidence retain accountable owners
through their specialist roles.

## Non-Goals

- Locking documents so only the owner can edit them.
- Treating file existence alone as readiness.
- Replacing project-specific flow overlays with one universal governance
  process.
