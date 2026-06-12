from pathlib import Path

import pytest

from agentic_mesh_v2.project_config import load_role_worker_config


def test_load_role_worker_config_resolves_safe_output_file_path(tmp_path: Path) -> None:
    project_file = tmp_path / "agentic-mesh" / "project.yaml"
    project_file.parent.mkdir()
    project_file.write_text(
        """
project_id: test-project
roles:
  product-manager:
    worker:
      adapter: safe-output-file
      path: workers/product-manager-calls.json
""",
        encoding="utf-8",
    )

    config = load_role_worker_config(project_file, role_id="product-manager")

    assert config["adapter"] == "safe-output-file"
    assert config["path"] == str(project_file.parent / "workers" / "product-manager-calls.json")


def test_load_role_worker_config_preserves_future_adapter_config(tmp_path: Path) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: test-project
roles:
  engineering:
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-team
""",
        encoding="utf-8",
    )

    config = load_role_worker_config(project_file, role_id="engineering")

    assert config == {
        "adapter": "codex-cli",
        "model": "codex",
        "auth": {"credential": "codex-team"},
    }


@pytest.mark.parametrize(
    ("body", "role_id", "message"),
    [
        ("[]", "product-manager", "project config must be a mapping"),
        ("project_id: test", "product-manager", "project config must define roles"),
        ("roles: {}", "product-manager", "does not define role"),
        ("roles:\n  product-manager: {}", "product-manager", "must define worker config"),
        (
            "roles:\n  product-manager:\n    worker:\n      model: codex",
            "product-manager",
            "requires adapter",
        ),
    ],
)
def test_load_role_worker_config_rejects_invalid_project_config(
    tmp_path: Path,
    body: str,
    role_id: str,
    message: str,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_role_worker_config(project_file, role_id=role_id)
