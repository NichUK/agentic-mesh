from __future__ import annotations

from dataclasses import dataclass

from agentic_mesh.models import AuthBinding, AuthMethod, RoleInstanceConfig


@dataclass(frozen=True)
class AuthInjectionPlan:
    role_instance_id: str
    adapter: str
    method: str | None
    category: str | None
    env_vars: list[str]
    secret_ref: str | None
    mount_ref: str | None
    redacted: bool = True


class AuthResolver:
    def __init__(self, auth_methods: dict[str, AuthMethod]) -> None:
        self.auth_methods = auth_methods

    def plan_for_instance(self, instance: RoleInstanceConfig) -> AuthInjectionPlan:
        binding = instance.override.worker.auth
        adapter = instance.override.worker.adapter
        if binding is None:
            return AuthInjectionPlan(
                role_instance_id=instance.instance_id,
                adapter=adapter,
                method=None,
                category=None,
                env_vars=[],
                secret_ref=None,
                mount_ref=None,
            )

        method = self._method_for(adapter, binding)
        return AuthInjectionPlan(
            role_instance_id=instance.instance_id,
            adapter=adapter,
            method=method.method_id,
            category=method.category,
            env_vars=method.env_vars,
            secret_ref=binding.secret_ref,
            mount_ref=binding.mount_ref,
        )

    def _method_for(self, adapter: str, binding: AuthBinding) -> AuthMethod:
        method = self.auth_methods[binding.method]
        if adapter not in method.applies_to:
            raise ValueError(
                f"Adapter {adapter} cannot use auth method {binding.method}"
            )
        return method
