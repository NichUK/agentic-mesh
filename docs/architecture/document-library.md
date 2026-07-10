# Document Library

Status: active V4 guidance

Date: 2026-06-04

## Purpose

The document library is a first-class project boundary. It is where durable
project knowledge, plans, evidence, reviews, and decisions are organized for
humans and agents.

Documents are the canonical institutional memory. Role memory is a derived,
source-linked cache that helps agents avoid rereading the entire library before
every task.

## Project Configuration

Projects declare a document library independently from the work workspace:

```yaml
document_library:
  backend: git
  root: ../../..
  structure_policy: togaf-sdlc-v1
  index_path: docs/00-index/document-library-manifest.json
  review_log_standard: same-document-review-log-v1
  versioning: backend
```

V4 supports the document library through an adapter boundary. The dogfood
project uses OneDrive so the same canonical files are visible to role agents,
the dashboard, Teams Shared Files, and humans. Versioning belongs to the
backend: Git history for Git-backed libraries and Microsoft version history for
OneDrive or SharePoint.

## Organization

The `togaf-sdlc-v1` policy uses numbered durable areas:

```text
000-index/
010-business/
020-architecture/
030-product/
040-delivery/
050-engineering/
060-quality/
070-operations/
080-release/
work-items/{work_item_id}/
```

Existing repositories may keep established folders such as `docs/product/` or
`docs/architecture/`. New project libraries should prefer the numbered layout
when starting clean.

Three-digit prefixes are mandatory for new ordered areas and documents. This
keeps `010`, `100`, and `110` in lifecycle order in filesystem, OneDrive, and
rendered index listings.

## Enterprise Architecture Portfolio

The TOGAF-aligned project portfolio lives under:

```text
020-architecture/enterprise/
  000-index.md
  010-architecture-principles.md
  020-architecture-vision.md
  030-business-capability-map.md
  040-target-operating-model.md
  050-business-architecture.md
  060-data-architecture.md
  070-application-architecture.md
  080-technology-architecture.md
  090-architecture-requirements.md
  100-standards-and-reference-architectures.md
  110-gap-analysis.md
  120-transition-architectures.md
  130-architecture-roadmap.md
  140-conformance-and-exceptions.md
  150-architecture-change-log.md
  160-architecture-decision-register.md
```

Enterprise Architect is accountable for the portfolio. Project configuration
names contributing roles and required sections for each document. Domain views
record baseline, target, gaps, decisions, dependencies, and freshness. A domain
may be declared not applicable only with an evidence-based reason and review
date; generated prose must not be used to disguise an unassessed domain.

## Slice Documentation

Every real work item must produce enterprise-grade documentation. The default
location for lifecycle evidence is:

```text
work-items/{work_item_id}/
```

Durable area documents such as `docs/product/`, `docs/architecture/`, ADRs,
standards, and operating guides are for evergreen knowledge. Agents should only
update those durable documents when the work intentionally changes durable
project knowledge. They must not use generic durable files as a dumping ground
for slice notes.

Slice documents should be ordered so the work item reads as a coherent dossier,
for example:

```text
work-items/{work_item_id}/010-business-brief.md
work-items/{work_item_id}/020-product-definition.md
work-items/{work_item_id}/030-experience-design.md
work-items/{work_item_id}/040-enterprise-alignment.md
work-items/{work_item_id}/050-solution-design.md
work-items/{work_item_id}/060-security-review.md
work-items/{work_item_id}/070-platform-readiness.md
work-items/{work_item_id}/080-implementation-plan.md
work-items/{work_item_id}/090-quality-plan.md
work-items/{work_item_id}/100-implementation-log.md
work-items/{work_item_id}/110-quality-evidence.md
work-items/{work_item_id}/120-documentation-readiness.md
work-items/{work_item_id}/130-delivery-readiness.md
work-items/{work_item_id}/140-release-record.md
```

Each slice document should include, where relevant:

- objective and business outcome
- scope and non-goals
- assumptions and dependencies
- decisions and rationale
- implementation or review evidence
- risks, controls, and residual concerns
- `## Review Log`
- next handoff, closure criteria, or release decision

## Naming

- Use lowercase kebab-case filenames.
- Prefix durable library areas with ordering numbers.
- Include `work_item_id` in slice-scoped artifacts.
- Keep one subject per file unless the document is an index or register.
- Do not encode status in filenames; use metadata and review logs.

## Metadata

Each accountable document should be discoverable through the generated library
manifest and project configuration. The minimum metadata is:

- document owner role
- contributing roles
- lifecycle state or states
- source work item when applicable
- review status
- canonical backend path or URL
- last review result

Enterprise architecture documents also record last-reviewed/freshness data,
baseline and target scope, and the decisions or work items that changed them.

The manifest is generated from configuration first and can later be enriched by
journal events, backend URLs, and review state.

## Review Logs

Markdown has no universal hidden comment standard that works well for rendered
documents. Agentic Mesh uses visible same-document review logs.

Every reviewable document should contain:

```markdown
## Review Log

### REV-0001 | qa-engineer | changes_requested

- Scope: Implementation plan, test hooks
- Comment: The plan does not expose enough state for BDD validation.
- Required change: Add observable outcomes for each acceptance criterion.
- Disposition: open
- Linked sub-slice: none
```

Review comments must be concrete enough for the accountable owner to accept,
reject, or turn into a sub-slice.

## Role Memory

Role memory is enabled per project:

```yaml
role_memory:
  enabled: true
  backend: filesystem
  root: memory/roles
  config_root: agentic-mesh/roles
  memory_filename: MEMORY.md
  provenance_required: true
  refresh_from_document_library: true
```

Role memory stores concise summaries of recurring decisions, domain facts, open
risks, stakeholder preferences, and previous handoffs. Every entry must cite
the source document, work item, or event that produced it.

The document library remains canonical. If memory and documents disagree, the
agent must trust the document library and refresh memory.

Each role should have a project-local folder under `config_root`, for example
`agentic-mesh/roles/product-manager/`. That folder can hold `role.yaml`,
`MEMORY.md`, and later role-specific tool or skill configuration. Runtime
prompts include this folder context plus recent same-conversation and linked
work-item thread context so short follow-up messages are interpreted against
the active feature rather than as isolated instructions.

## Team Meshes

Peer team meshes may share roles. A role such as `enterprise-architect` or
`technical-writer` should not duplicate its long-term memory just because it
participates in more than one team.

Cross-mesh handoff creates a new linked work item in the receiving mesh. The
receiving mesh applies its normal flow and review gates, scoped by affected
roles where possible.
