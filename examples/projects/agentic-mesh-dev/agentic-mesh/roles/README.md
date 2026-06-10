# Role Runtime Folders

This folder contains project-local role configuration and role memory for the
Agentic Mesh development project.

Each role folder may contain:

- `role.yaml`: project-local runtime notes for the role.
- `MEMORY.md`: concise source-linked role memory shared by instances of the
  same role.

The document library, ADRs, work-item artifacts, and event journal remain
canonical. Role memory is a cache that helps fresh worker processes recover
context quickly.

