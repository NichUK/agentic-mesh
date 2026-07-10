from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.flow import render_flow_mermaid


EA_ROOT = Path("020-architecture/enterprise")
MANIFEST_PATH = Path("000-index/document-library-manifest.json")


@dataclass(frozen=True)
class PortfolioMaterializationResult:
    created: tuple[str, ...]
    preserved: tuple[str, ...]
    migrated: tuple[str, ...]
    manifest_path: str


def materialize_enterprise_architecture_portfolio(
    *,
    project_config: V4ProjectConfig,
    document_root: Path,
    legacy_root: Path | None = None,
) -> PortfolioMaterializationResult:
    document_root = document_root.resolve()
    accountabilities = {
        path: details
        for path, details in (project_config.document_accountabilities or {}).items()
        if path.startswith(f"{EA_ROOT.as_posix()}/") and details.get("owner_role") == "enterprise-architect"
    }
    if not accountabilities:
        raise ValueError("project config has no Enterprise Architecture document accountabilities")

    created: list[str] = []
    preserved: list[str] = []
    migrated: list[str] = []
    for relative_path, details in sorted(accountabilities.items()):
        target = _safe_target(document_root=document_root, relative_path=relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            preserved.append(relative_path)
            continue
        target.write_text(
            _render_document(
                path=relative_path,
                details=details,
                portfolio_paths=tuple(sorted(accountabilities)),
            ),
            encoding="utf-8",
        )
        created.append(relative_path)

    if legacy_root is not None:
        legacy_mappings = {
            legacy_root / "docs" / "architecture" / "enterprise-notes.md": document_root / EA_ROOT / "020-architecture-vision.md",
            legacy_root / "docs" / "architecture" / "decisions.md": document_root / EA_ROOT / "160-architecture-decision-register.md",
        }
        for source, target in legacy_mappings.items():
            if _append_legacy_source(source=source, target=target):
                migrated.append(str(source))

    manifest_target = _safe_target(document_root=document_root, relative_path=MANIFEST_PATH.as_posix())
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    manifest_target.write_text(
        json.dumps(
            {
                "project_id": project_config.project_id,
                "structure_policy": project_config.document_structure_policy,
                "generated_at": datetime.now(UTC).isoformat(),
                "documents": [
                    {
                        "path": path,
                        "owner_role": details.get("owner_role"),
                        "accountability": details.get("accountability"),
                        "contributing_roles": details.get("contributing_roles", []),
                        "required_sections": details.get("required_sections", []),
                        "review_on_contribution": bool(details.get("review_on_contribution", False)),
                    }
                    for path, details in sorted((project_config.document_accountabilities or {}).items())
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    flow_path = _safe_target(document_root=document_root, relative_path="000-index/sdlc-flow.mmd")
    flow_path.write_text(render_flow_mermaid(project_config.flow or {}), encoding="utf-8")
    return PortfolioMaterializationResult(
        created=tuple(created),
        preserved=tuple(preserved),
        migrated=tuple(migrated),
        manifest_path=MANIFEST_PATH.as_posix(),
    )


def _render_document(*, path: str, details: dict[str, Any], portfolio_paths: tuple[str, ...]) -> str:
    title = _title_from_path(path)
    lines = [
        f"# {title}",
        "",
        "- Owner role: enterprise-architect",
        "- Status: unassessed",
        "- Last reviewed: not yet reviewed",
        "- Source: project document accountability configuration",
        "",
    ]
    if path.endswith("/000-index.md"):
        lines.extend(
            [
                "## Purpose",
                "",
                "This index is the entry point to the durable enterprise architecture portfolio.",
                "",
                "## Ownership",
                "",
                "Enterprise Architect is accountable. Named contributing roles provide domain evidence and review.",
                "",
                "## Portfolio Map",
                "",
            ]
        )
        for item in portfolio_paths:
            if item == path:
                continue
            item_path = Path(item)
            lines.append(f"- [{_title_from_path(item)}]({item_path.name})")
        lines.extend(
            [
                "",
                "## Review Cadence",
                "",
                "Review after material releases, approved architecture exceptions, strategic changes, and scheduled architecture-health checks.",
            ]
        )
    else:
        lines.extend(
            [
                "## Assessment Status",
                "",
                "Initial assessment is pending. This scaffold contains no inferred architecture. "
                "The accountable owner must populate it from evidence; any not-applicable disposition "
                "must record the scope-specific rationale and review date.",
                "",
            ]
        )
        for section in details.get("required_sections") or []:
            lines.extend(
                [
                    f"## {_heading(str(section))}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _safe_target(*, document_root: Path, relative_path: str) -> Path:
    target = (document_root / relative_path).resolve()
    if target != document_root and document_root not in target.parents:
        raise ValueError(f"document path escapes document root: {relative_path}")
    return target


def _title_from_path(path: str) -> str:
    stem = Path(path).stem
    stem = re.sub(r"^\d{3}-", "", stem)
    return _heading(stem)


def _heading(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", "-").split("-") if part)


def _append_legacy_source(*, source: Path, target: Path) -> bool:
    if not source.exists() or not target.exists():
        return False
    marker = f"Imported source: `{source.as_posix()}`"
    current = target.read_text(encoding="utf-8")
    if marker in current:
        return False
    source_text = source.read_text(encoding="utf-8").strip()
    if not source_text:
        return False
    target.write_text(
        current.rstrip()
        + "\n\n## Imported Source Material\n\n"
        + marker
        + "\n\n"
        + source_text
        + "\n",
        encoding="utf-8",
    )
    return True
