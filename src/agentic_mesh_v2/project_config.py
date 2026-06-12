from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_role_worker_config(project_file: Path, *, role_id: str) -> dict[str, Any]:
    with project_file.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("project config must be a mapping")
    roles = raw.get("roles")
    if not isinstance(roles, dict):
        raise ValueError("project config must define roles")
    role = roles.get(role_id)
    if not isinstance(role, dict):
        raise ValueError(f"project config does not define role `{role_id}`")
    worker = role.get("worker")
    if not isinstance(worker, dict):
        raise ValueError(f"role `{role_id}` must define worker config")
    adapter = worker.get("adapter")
    if not isinstance(adapter, str) or not adapter.strip():
        raise ValueError(f"role `{role_id}` worker config requires adapter")

    config = dict(worker)
    if adapter == "safe-output-file":
        path = config.get("path")
        if isinstance(path, str) and path.strip():
            worker_path = Path(path)
            if not worker_path.is_absolute():
                worker_path = project_file.parent / worker_path
            config["path"] = str(worker_path)
    return config
