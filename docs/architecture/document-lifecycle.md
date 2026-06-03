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

## Enterprise Architecture Pattern Alignment

This maps cleanly to enterprise architecture and governance practices:

- architecture records have accountable architecture owners
- decision records have accountable decision owners
- security artifacts have accountable security owners
- implementation evidence has accountable engineering owners
- test evidence has accountable QA owners
- release records have accountable release owners

Future stock flow packs can align these accountabilities with TOGAF-style
architecture governance, ITIL-style change enablement, data governance flows,
security/compliance review, and other enterprise operating patterns.

## Non-Goals

- Locking documents so only the owner can edit them.
- Treating file existence alone as readiness.
- Replacing project-specific flow overlays with one universal governance
  process.
