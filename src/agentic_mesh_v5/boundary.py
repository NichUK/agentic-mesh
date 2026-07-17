from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


FORBIDDEN_RUNTIME_IMPORTS = (
    "agentic_mesh_v4",
    "agentic_mesh_v3",
    "agentic_mesh_v2",
)


@dataclass(frozen=True, slots=True)
class BoundaryViolation:
    path: Path
    line: int
    module: str


class RuntimeBoundaryError(RuntimeError):
    def __init__(self, violations: tuple[BoundaryViolation, ...]) -> None:
        self.violations = violations
        details = ", ".join(
            f"{item.path}:{item.line} imports {item.module}" for item in violations
        )
        super().__init__(f"V5 runtime boundary violation: {details}")


class InvalidPackageRootError(ValueError):
    pass


def _is_forbidden(module: str) -> bool:
    return any(
        module == root or module.startswith(f"{root}.")
        for root in FORBIDDEN_RUNTIME_IMPORTS
    )


def find_runtime_boundary_violations(
    package_root: Path | None = None,
) -> tuple[BoundaryViolation, ...]:
    root = package_root or Path(__file__).resolve().parent
    if not root.exists() or not root.is_dir():
        raise InvalidPackageRootError(
            f"package root must be an existing directory: {root}"
        )
    violations: list[BoundaryViolation] = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            for module in modules:
                if _is_forbidden(module):
                    violations.append(
                        BoundaryViolation(path=path, line=node.lineno, module=module)
                    )

    return tuple(violations)


def require_clean_runtime_boundary(package_root: Path | None = None) -> None:
    violations = find_runtime_boundary_violations(package_root)
    if violations:
        raise RuntimeBoundaryError(violations)
