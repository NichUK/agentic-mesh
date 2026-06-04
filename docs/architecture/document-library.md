# Document Library

Status: draft design

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

Supported v1 backends are `git` and `filesystem`. `onedrive` and `sharepoint`
are reserved adapter targets. Versioning belongs to the backend: Git history
for Git-backed libraries and Microsoft version history for OneDrive or
SharePoint.

## Organization

The `togaf-sdlc-v1` policy uses numbered durable areas:

```text
00-index/
10-business/
20-architecture/
30-product/
40-delivery/
50-engineering/
60-quality/
70-operations/
80-release/
work-items/{work_item_id}/
```

Existing repositories may keep established folders such as `docs/product/` or
`docs/architecture/`. New project libraries should prefer the numbered layout
when starting clean.

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

The manifest is generated from configuration first and can later be enriched by
journal events, backend URLs, and review state.

## Review Logs

Markdown has no universal hidden comment standard that works well for rendered
documents. Agentic Mesh v1 uses visible same-document review logs.

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
  provenance_required: true
  refresh_from_document_library: true
```

Role memory stores concise summaries of recurring decisions, domain facts, open
risks, stakeholder preferences, and previous handoffs. Every entry must cite
the source document, work item, or event that produced it.

The document library remains canonical. If memory and documents disagree, the
agent must trust the document library and refresh memory.

## Team Meshes

Peer team meshes may share roles. A role such as `enterprise-architect` or
`technical-writer` should not duplicate its long-term memory just because it
participates in more than one team.

Cross-mesh handoff creates a new linked work item in the receiving mesh. The
receiving mesh applies its normal flow and review gates, scoped by affected
roles where possible.
