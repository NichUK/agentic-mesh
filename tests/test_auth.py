from pathlib import Path

from agentic_mesh.auth import AuthResolver
from agentic_mesh.config import load_mesh_config


def test_auth_resolver_returns_redacted_injection_plan() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    resolver = AuthResolver(mesh_config.auth_methods)
    instance = mesh_config.instances["agentic-mesh-dev.security-architect.1"]

    plan = resolver.plan_for_instance(instance)

    assert plan.role_instance_id == "agentic-mesh-dev.security-architect.1"
    assert plan.adapter == "codex-cli"
    assert plan.method == "codex_access_token"
    assert plan.category == "access_token"
    assert plan.env_vars == ["CODEX_ACCESS_TOKEN"]
    assert plan.secret_ref == "codex-agentic-mesh-dev-security-architect-token"
    assert plan.mount_ref is None
    assert plan.redacted is True


def test_auth_resolver_handles_ux_codex_token_auth() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    resolver = AuthResolver(mesh_config.auth_methods)
    instance = mesh_config.instances["agentic-mesh-dev.ux-designer.1"]

    plan = resolver.plan_for_instance(instance)

    assert plan.method == "codex_access_token"
    assert plan.env_vars == ["CODEX_ACCESS_TOKEN"]
    assert plan.secret_ref == "codex-agentic-mesh-dev-ux-designer-token"
    assert plan.mount_ref is None
