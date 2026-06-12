from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CAPABILITY_STATUSES = {"granted", "missing", "revoked"}
PERMISSION_PHASES = {"setup", "runtime"}
PERMISSION_CONSENT_TYPES = {"delegated", "application", "resource_specific", "bot"}

DEFAULT_GRANTED_CAPABILITIES = {
    "app_installation",
    "tenant_consent",
    "team_binding",
    "channel_binding",
    "role_identity_binding",
    "member_metadata_access",
    "send_capability",
    "response_submission",
}

BROAD_GRAPH_PERMISSION_MARKERS = (
    "ChannelMessage.Read.All",
    "Chat.Read.All",
    "Chat.ReadWrite.All",
    "Team.ReadBasic.All",
    "Directory.Read.All",
    "User.Read.All",
    "Group.Read.All",
)

SECRET_FIELD_NAMES = {
    "access_token",
    "api_key",
    "client_secret",
    "credential_value",
    "password",
    "refresh_token",
    "secret",
    "secret_value",
    "token",
    "token_value",
}

SECRET_FIELD_SUFFIXES = ("_secret", "_token", "_api_key", "_password")


@dataclass(frozen=True)
class PermissionDeclaration:
    permission: str
    phase: str
    consent_type: str
    status: str
    required: bool
    broad_graph: bool
    approval_ref: str | None
    rationale: str | None


@dataclass(frozen=True)
class ConnectorPermissionModel:
    capabilities: dict[str, str]
    declarations: tuple[PermissionDeclaration, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ConnectorPermissionModel:
        raw = raw or {}
        capabilities = {capability: "granted" for capability in DEFAULT_GRANTED_CAPABILITIES}
        supplied_capabilities = raw.get("capabilities", {})
        if supplied_capabilities is not None:
            if not isinstance(supplied_capabilities, dict):
                raise ValueError("`permission_validation.capabilities` must be a mapping")
            for key, value in supplied_capabilities.items():
                capability = _required_key(key, "permission capability")
                capabilities[capability] = _normalize_status(value, f"capability `{capability}`")
        declarations = tuple(_permission_declarations(raw.get("permissions", ())))
        return cls(capabilities=capabilities, declarations=declarations)

    def status_for(self, capability: str) -> str:
        if capability in self.capabilities:
            return self.capabilities[capability]
        if ":" in capability:
            generic = capability.split(":", 1)[0]
            return self.capabilities.get(generic, "granted")
        return "granted"

    def failing_capabilities(self, capabilities: list[str]) -> list[tuple[str, str]]:
        failures: list[tuple[str, str]] = []
        for capability in capabilities:
            status = self.status_for(capability)
            if status != "granted":
                failures.append((capability, status))
        return failures

    def failing_declarations(self) -> list[PermissionDeclaration]:
        failures: list[PermissionDeclaration] = []
        for declaration in self.declarations:
            if declaration.required and declaration.status != "granted":
                failures.append(declaration)
            if declaration.broad_graph and not declaration.approval_ref:
                failures.append(declaration)
        return failures


class PermissionValidationFailure(RuntimeError):
    def __init__(self, message: str, *, failures: list[tuple[str, str]]) -> None:
        super().__init__(message)
        self.failures = failures


def reject_inline_secrets(raw: Any, *, path: str = "connector") -> None:
    if isinstance(raw, dict):
        for key, value in raw.items():
            key_text = str(key)
            normalized = key_text.strip().casefold()
            current_path = f"{path}.{key_text}"
            if _is_secret_field(normalized):
                if value not in (None, "", [], {}):
                    raise ValueError(
                        f"`{current_path}` looks like inline credential material; use a secret reference instead"
                    )
            reject_inline_secrets(value, path=current_path)
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            reject_inline_secrets(value, path=f"{path}[{index}]")


def startup_capabilities_for(
    *,
    project_team_ref: str,
    default_project_channel_ref: str,
    role_ids: list[str],
    channel_refs: list[str],
) -> list[str]:
    capabilities = [
        "app_installation",
        "tenant_consent",
        "team_binding",
        f"team_binding:{project_team_ref}",
        "channel_binding",
        f"channel_binding:{default_project_channel_ref}",
        "member_metadata_access",
        "send_capability",
    ]
    for channel_ref in channel_refs:
        capabilities.append(f"channel_binding:{channel_ref}")
    for role_id in role_ids:
        capabilities.append(f"role_identity_binding:{role_id}")
    return capabilities


def runtime_capabilities_for_receive(*, source_type: str, conversation_ref: str) -> list[str]:
    capabilities = ["app_installation", "tenant_consent", "member_metadata_access"]
    if source_type != "dm":
        capabilities.extend(["channel_binding", f"channel_binding:{conversation_ref}"])
    return capabilities


def runtime_capabilities_for_send() -> list[str]:
    return ["app_installation", "tenant_consent", "send_capability"]


def runtime_capabilities_for_response() -> list[str]:
    return ["app_installation", "tenant_consent", "member_metadata_access", "response_submission"]


def _permission_declarations(value: object) -> list[PermissionDeclaration]:
    if value in (None, ()):
        return []
    if not isinstance(value, list):
        raise ValueError("`permission_validation.permissions` must be a list")
    declarations: list[PermissionDeclaration] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("permission declarations must be objects")
        permission = _required_string(item, "permission")
        phase = _required_string(item, "phase")
        if phase not in PERMISSION_PHASES:
            raise ValueError(f"unknown permission phase `{phase}`")
        consent_type = _required_string(item, "consent_type")
        if consent_type not in PERMISSION_CONSENT_TYPES:
            raise ValueError(f"unknown consent type `{consent_type}`")
        status = _normalize_status(item.get("status", "granted"), f"permission `{permission}`")
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise ValueError("permission declaration `required` must be a boolean")
        broad_graph = item.get("broad_graph")
        if broad_graph is None:
            broad_graph = any(marker.casefold() == permission.casefold() for marker in BROAD_GRAPH_PERMISSION_MARKERS)
        if not isinstance(broad_graph, bool):
            raise ValueError("permission declaration `broad_graph` must be a boolean")
        approval_ref = item.get("approval_ref")
        if approval_ref is not None:
            approval_ref = str(approval_ref).strip() or None
        declarations.append(
            PermissionDeclaration(
                permission=permission,
                phase=phase,
                consent_type=consent_type,
                status=status,
                required=required,
                broad_graph=broad_graph,
                approval_ref=approval_ref,
                rationale=str(item["rationale"]).strip() if item.get("rationale") else None,
            )
        )
    return declarations


def _normalize_status(value: object, label: str) -> str:
    if isinstance(value, bool):
        return "granted" if value else "missing"
    status = str(value or "").strip().casefold()
    if status not in CAPABILITY_STATUSES:
        raise ValueError(f"{label} has unknown status `{value}`")
    return status


def _required_key(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} keys must be non-empty strings")
    return value.strip()


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{key}` must be a non-empty string")
    return value.strip()


def _is_secret_field(normalized_key: str) -> bool:
    return normalized_key in SECRET_FIELD_NAMES or any(
        normalized_key.endswith(suffix) for suffix in SECRET_FIELD_SUFFIXES
    )
