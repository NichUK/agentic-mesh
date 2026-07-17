# AMV5-001 Clean V5 Runtime Boundary

## Outcome

Create a standalone `agentic_mesh_v5` package, module entry point, and console
entry point without changing or importing the running V4 implementation.

## Scope

- add the V5 package and bootstrap status command;
- add an automated source check that rejects V2, V3, and V4 runtime imports;
- verify V5 import and CLI startup in isolated Python subprocesses;
- retain V4 as the running production baseline until the approved cutover.

Configuration packages, persistence, workers, queues, and V4 asset reuse are
separate stories. They must not be pulled into this bootstrap slice.

## Acceptance Criteria

- `python -m agentic_mesh_v5 --json status` starts without loading V4;
- `agentic-mesh-v5` is installed as a console entry point;
- the V5 source tree passes the runtime-boundary check;
- synthetic V2, V3, and V4 imports fail the boundary check;
- the package boundary and temporary V4 coexistence are documented.

## Test Evidence

Run:

```powershell
pytest -q tests/test_v5_runtime_boundary.py
python -m agentic_mesh_v5 --json boundary-check
python -m agentic_mesh_v5 --json status
```
