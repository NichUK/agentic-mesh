# Project Import Discovery

AMV5-053 introduces a read-only discovery boundary for proposed project
imports. It inventories every declared Git repository, document root, and
external binding before a project manifest can be previewed or activated.

Discovery is deliberately separate from the active project registry. It does
not register a project, claim resources, create branches, alter documents,
update ADO or Teams, or generate backlog candidates. The deterministic report
is an input to the completeness-question and sponsor-preview work in
AMV5-054/055.

The service:

- attempts every declared source, even after another source is unavailable;
- uses read-only repository and document adapter contracts;
- summarises large document trees without retaining document content;
- records bounded samples while retaining complete aggregate counts;
- detects duplicate identifiers and shared locators as explicit conflicts;
- reports inaccessible, invalid, or safety-limited sources without leaking
  credentials or transport details; and
- produces a canonical digest over the request and result so later stages can
  pin the exact discovery evidence they reviewed.

`attempted_all` means every declaration received a result. `complete` is
stricter: every source was accessible, fully enumerated, and conflict-free.
An incomplete report is still useful evidence, but AMV5-054 must resolve its
material gaps before AMV5-055 can offer activation.

## Acceptance Criteria

- Multiple repositories, document roots, and bindings are represented in one
  project-neutral report.
- Git discovery uses non-mutating remote inspection and document discovery
  calls only the `list` operation of a scoped `DocumentStore`.
- A failure in one source does not prevent any other declared source from
  being attempted.
- Large paginated document libraries are counted completely within the
  configured safety limit, with only a bounded path sample retained.
- Duplicate source identifiers and resource locators make the report
  incomplete and identify every affected declaration.
- Credentials, tokens, document content, and raw connector errors do not enter
  the report.
- Repeating discovery with identical inputs and adapter results produces the
  same canonical digest.
- Discovery has no activation or mutation dependency and contains no D8Aroom,
  Quantauma, or other project-specific path.
