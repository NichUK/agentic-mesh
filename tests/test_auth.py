from pathlib import Path

from agentic_mesh.auth import AuthResolver
from agentic_mesh.config import load_mesh_config


def test_auth_resolver_returns_redacted_injection_plan() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    resolver = AuthResolver(mesh_config.auth_methods)
    instance = mesh_config.instances["agentic-mesh-dev.security-architect.1"]

    plan = resolver.plan_for_instance(instance)

    assert plan.role_instance_id == "agentic-mesh-dev.security-architect.1"
    assert plan.adapter == "claude-code"
    assert plan.method == "claude_code_oauth_token"
    assert plan.category == "oauth_token"
    assert plan.env_vars == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert plan.secret_ref == "claude-code-agentic-mesh-security-token"
    assert plan.mount_ref is None
    assert plan.redacted is True


def test_auth_resolver_handles_mount_based_auth() -> None:
    mesh_config = load_mesh_config(Path.cwd())
    resolver = AuthResolver(mesh_config.auth_methods)
    instance = mesh_config.instances["agentic-mesh-dev.ux-designer.1"]

    plan = resolver.plan_for_instance(instance)

    assert plan.method == "codex_oauth_cache"
    assert plan.env_vars == ["CODEX_HOME"]
    assert plan.secret_ref is None
    assert plan.mount_ref == "local-codex-ux-designer-home"
