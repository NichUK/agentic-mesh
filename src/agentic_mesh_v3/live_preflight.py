from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Callable

from agentic_mesh_v3.broker import build_broker_adapter
from agentic_mesh_v3.project_config import V3ProjectConfig


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    passed: bool
    summary: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "summary": self.summary,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LivePreflightResult:
    passed: bool
    checks: tuple[PreflightCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }


def run_live_preflight(
    *,
    project_config: V3ProjectConfig,
    env: dict[str, str] | None = None,
    check_broker: bool = False,
    check_document_library: bool = False,
    document_exists: Callable[[str], bool] | None = None,
) -> LivePreflightResult:
    env = env if env is not None else dict(os.environ)
    checks: list[PreflightCheck] = []
    checks.append(_roles_check(project_config))
    checks.append(_document_config_check(project_config))
    checks.extend(_document_env_checks(project_config, env))
    checks.append(_teams_config_check(project_config))
    checks.extend(_teams_env_checks(project_config, env))
    checks.append(_broker_config_check(project_config, check_live=check_broker))
    if check_broker:
        checks.append(_broker_live_check(project_config))
    checks.extend(_deployment_target_checks(project_config))
    checks.extend(_stakeholder_contact_checks(project_config))
    if check_document_library:
        checks.append(_document_library_live_check(document_exists))
    return LivePreflightResult(
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
    )


def _roles_check(project_config: V3ProjectConfig) -> PreflightCheck:
    required_roles = {"product-manager", "engineering", "qa-engineer", "release-manager", "project-manager"}
    configured = {role.role_id for role in project_config.roles}
    missing = sorted(required_roles - configured)
    if missing:
        return PreflightCheck(
            "roles.required",
            False,
            "Missing required V3 dogfood roles.",
            ", ".join(missing),
        )
    return PreflightCheck("roles.required", True, "Required V3 dogfood roles are configured.")


def _document_config_check(project_config: V3ProjectConfig) -> PreflightCheck:
    docs = project_config.document_library
    adapter = docs.adapter.casefold().replace("_", "-")
    if adapter in {"onedrive", "sharepoint"}:
        if not docs.drive_id:
            return PreflightCheck(
                "documents.config",
                False,
                "OneDrive/SharePoint document library needs a drive_id.",
            )
        if docs.root_path.rstrip("/") != "/documents":
            return PreflightCheck(
                "documents.config",
                False,
                "V3 dogfood documents should be mounted in Teams Shared Files under /documents.",
                f"Configured root_path: {docs.root_path}",
            )
        return PreflightCheck(
            "documents.config",
            True,
            "OneDrive/SharePoint document library is configured for /documents.",
            f"drive_id={_redact(docs.drive_id)}",
        )
    if adapter in {"local", "filesystem", "file", "git"}:
        if docs.root is None:
            return PreflightCheck("documents.config", False, "Filesystem document library needs a root.")
        return PreflightCheck("documents.config", True, "Filesystem document library is configured.", str(docs.root))
    return PreflightCheck("documents.config", False, "Unsupported document library adapter.", docs.adapter)


def _document_env_checks(project_config: V3ProjectConfig, env: dict[str, str]) -> list[PreflightCheck]:
    adapter = project_config.document_library.adapter.casefold().replace("_", "-")
    if adapter not in {"onedrive", "sharepoint"}:
        return []
    return [
        _required_env_check(env, "AGENTIC_MESH_ONEDRIVE_TOKEN", "documents.env"),
        _required_scope_check(
            env,
            "AGENTIC_MESH_ONEDRIVE_TOKEN",
            "documents.env.scopes",
            any_of={"Files.ReadWrite.All", "Sites.ReadWrite.All"},
            purpose="write Teams/OneDrive document-library artifacts",
        ),
    ]


def _teams_config_check(project_config: V3ProjectConfig) -> PreflightCheck:
    adapter = (project_config.teams_connector.adapter or "").casefold().replace("_", "-")
    if adapter in {"", "none"}:
        return PreflightCheck("teams.config", False, "Teams connector is not configured.")
    if adapter in {"local", "local-teams", "in-memory"}:
        return PreflightCheck("teams.config", True, "Local Teams-shaped connector is configured.")
    if adapter == "teams-bot-connector":
        role_bots = [role for role in project_config.roles if role.messaging_identity.display_name]
        missing_refs = [
            role.role_id
            for role in role_bots
            if not role.messaging_identity.bot_id_ref or not role.messaging_identity.secret_ref
        ]
        if missing_refs:
            return PreflightCheck(
                "teams.config",
                False,
                "Bot Framework Teams connector has role bots without bot_id_ref/secret_ref.",
                ", ".join(missing_refs),
            )
        if not role_bots:
            return PreflightCheck("teams.config", False, "Bot Framework Teams connector has no role bot identities.")
        return PreflightCheck(
            "teams.config",
            True,
            "Bot Framework Teams connector and role bot identities are configured.",
            ", ".join(role.role_id for role in role_bots),
        )
    if adapter in {"graph", "microsoft-graph", "teams-graph"}:
        role_bots = [role for role in project_config.roles if role.messaging_identity.display_name]
        if not role_bots:
            return PreflightCheck("teams.config", False, "Graph Teams connector has no role bot identities.")
        return PreflightCheck(
            "teams.config",
            True,
            "Graph Teams connector and role bot identities are configured.",
            ", ".join(role.role_id for role in role_bots),
        )
    return PreflightCheck("teams.config", False, "Unsupported Teams connector adapter.", adapter)


def _teams_env_checks(project_config: V3ProjectConfig, env: dict[str, str]) -> list[PreflightCheck]:
    adapter = (project_config.teams_connector.adapter or "").casefold().replace("_", "-")
    if adapter == "teams-bot-connector":
        checks = [
            _required_env_check(env, "AGENTIC_MESH_TEAMS_BOT_SERVICE_URL", "teams.env.bot_service_url"),
            _required_env_check(env, "AGENTIC_MESH_TENANT_ID", "teams.env.tenant"),
        ]
        configured_roles: list[str] = []
        missing_roles: list[str] = []
        for role in project_config.roles:
            messaging = role.messaging_identity
            if not messaging.display_name:
                continue
            app_key = _env_name_for_ref(messaging.bot_id_ref) if messaging.bot_id_ref else ""
            secret_key = _env_name_for_ref(messaging.secret_ref) if messaging.secret_ref else ""
            if app_key and secret_key and env.get(app_key) and env.get(secret_key):
                configured_roles.append(role.role_id)
            else:
                missing_roles.append(role.role_id)
        if configured_roles:
            checks.append(
                PreflightCheck(
                    "teams.env.role_bot_coverage",
                    True,
                    "At least one Bot Framework role identity is configured for agent-owned Teams messaging.",
                    f"configured={', '.join(configured_roles)}; missing={', '.join(missing_roles) or 'none'}",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    "teams.env.role_bot_coverage",
                    False,
                    "At least one Bot Framework role identity must be configured for agent-owned Teams messaging.",
                    f"missing={', '.join(missing_roles) or 'all'}",
                )
            )
        return checks
    if adapter not in {"graph", "microsoft-graph", "teams-graph"}:
        return []
    checks = [
        _required_env_check(env, "AGENTIC_MESH_TEAMS_TOKEN", "teams.env.token"),
        _required_scope_check(
            env,
            "AGENTIC_MESH_TEAMS_TOKEN",
            "teams.env.scopes.chat",
            any_of={"Chat.Create", "Chat.ReadWrite"},
            purpose="create or reuse Teams direct chats",
        ),
        _required_scope_check(
            env,
            "AGENTIC_MESH_TEAMS_TOKEN",
            "teams.env.scopes.send",
            any_of={"ChatMessage.Send", "ChannelMessage.Send"},
            purpose="send stakeholder-facing Teams messages",
        ),
        _required_env_check(env, "AGENTIC_MESH_TEAMS_SENDER_USER_ID", "teams.env.sender"),
    ]
    checks.append(_teams_sender_is_not_sponsor_check(project_config, env))
    return checks


def _env_name_for_ref(ref: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in ref).upper()


def _teams_sender_is_not_sponsor_check(project_config: V3ProjectConfig, env: dict[str, str]) -> PreflightCheck:
    sponsor = next(
        (contact for contact in project_config.stakeholder_contacts if contact.contact_id == "sponsor"),
        None,
    )
    sender_user_id = (env.get("AGENTIC_MESH_TEAMS_SENDER_USER_ID") or "").strip()
    if sponsor is None or not sender_user_id:
        return PreflightCheck(
            "teams.env.sender_not_sponsor",
            False,
            "Teams sender/sponsor separation cannot be verified until sender and sponsor are configured.",
        )
    sponsor_user_id = _user_id_from_target_ref(sponsor.target_ref)
    if sponsor_user_id is None:
        return PreflightCheck(
            "teams.env.sender_not_sponsor",
            True,
            "Sponsor target is not a Teams user target; delegated self-DM guard is not applicable.",
            sponsor.target_ref,
        )
    if sponsor_user_id == sender_user_id:
        return PreflightCheck(
            "teams.env.sender_not_sponsor",
            False,
            "Teams delegated sender must not be the same user as the sponsor.",
            "Configure a distinct agent/bot sender, a real bot/proactive messaging path, or an explicit known-safe chat target.",
        )
    return PreflightCheck(
        "teams.env.sender_not_sponsor",
        True,
        "Teams delegated sender is distinct from the sponsor user target.",
    )


def _broker_config_check(project_config: V3ProjectConfig, *, check_live: bool) -> PreflightCheck:
    broker = project_config.broker
    adapter = broker.adapter.casefold().replace("_", "-")
    if adapter == "nats-jetstream" and not broker.servers:
        return PreflightCheck("broker.config", False, "NATS JetStream broker needs servers.")
    detail = f"adapter={broker.adapter}, stream={broker.stream}"
    if broker.servers:
        detail = f"{detail}, servers={broker.servers}"
    if check_live:
        detail = f"{detail}, live check requested"
    return PreflightCheck("broker.config", True, "Broker configuration is present.", detail)


def _broker_live_check(project_config: V3ProjectConfig) -> PreflightCheck:
    try:
        broker = build_broker_adapter(adapter=project_config.broker.adapter, servers=project_config.broker.servers)
        subjects = ["project.context"]
        for role in project_config.roles:
            subjects.append(f"agent.{role.role_id}")
            subjects.append(f"agent.{role.role_id}.relevance")
        broker.ensure_stream(project_config.broker.stream, subjects)
        depth = broker.depth(project_config.broker.stream)
    except Exception as exc:  # pragma: no cover - exercised with real broker failures
        return PreflightCheck("broker.live", False, "Broker live check failed.", str(exc))
    return PreflightCheck("broker.live", True, "Broker stream is reachable.", f"pending={depth.pending}")


def _deployment_target_checks(project_config: V3ProjectConfig) -> list[PreflightCheck]:
    if not project_config.release_deployment_targets:
        return [PreflightCheck("deployment.targets", False, "No release deployment targets are configured.")]
    checks: list[PreflightCheck] = []
    for target in project_config.release_deployment_targets:
        if target.target_type.casefold().replace("_", "-") != "command":
            checks.append(
                PreflightCheck(
                    f"deployment.target.{target.target_id}",
                    True,
                    "Non-command deployment target is configured.",
                    target.target_type,
                )
            )
            continue
        if not target.command:
            checks.append(
                PreflightCheck(
                    f"deployment.target.{target.target_id}",
                    False,
                    "Command deployment target has no command.",
                )
            )
            continue
        cwd_detail = ""
        if target.working_directory is not None:
            cwd_detail = f" cwd={target.working_directory}"
            if not target.working_directory.exists():
                checks.append(
                    PreflightCheck(
                        f"deployment.target.{target.target_id}",
                        False,
                        "Command deployment target working directory does not exist.",
                        str(target.working_directory),
                    )
                )
                continue
        executable = target.command[0]
        if Path(executable).is_absolute():
            executable_ok = Path(executable).exists()
        else:
            executable_ok = shutil.which(executable) is not None
        if not executable_ok:
            checks.append(
                PreflightCheck(
                    f"deployment.target.{target.target_id}",
                    False,
                    "Command deployment executable is not available.",
                    executable,
                )
            )
            continue
        checks.append(
            PreflightCheck(
                f"deployment.target.{target.target_id}",
                True,
                "Command deployment target is runnable by this host.",
                f"command={' '.join(target.command)}{cwd_detail}",
            )
        )
    return checks


def _stakeholder_contact_checks(project_config: V3ProjectConfig) -> list[PreflightCheck]:
    if not project_config.stakeholder_contacts:
        return [PreflightCheck("stakeholders.sponsor", False, "No stakeholder contacts are configured.")]
    checks = []
    sponsor = next(
        (contact for contact in project_config.stakeholder_contacts if contact.contact_id == "sponsor"),
        project_config.stakeholder_contacts[0],
    )
    if sponsor.connector != "teams":
        checks.append(
            PreflightCheck(
                "stakeholders.sponsor",
                False,
                "Sponsor contact must use Teams for this live dogfood proof.",
                sponsor.connector,
            )
        )
    elif not _target_ref_has_value(sponsor.target_ref):
        checks.append(PreflightCheck("stakeholders.sponsor", False, "Sponsor contact target_ref is empty."))
    else:
        checks.append(
            PreflightCheck(
                "stakeholders.sponsor",
                True,
                "Sponsor Teams contact is configured.",
                f"{sponsor.display_name} -> {sponsor.target_ref}",
            )
        )
    return checks


def _target_ref_has_value(target_ref: str) -> bool:
    normalized = target_ref.strip()
    if not normalized:
        return False
    if ":" not in normalized:
        return True
    _scheme, value = normalized.split(":", 1)
    return bool(value.strip())


def _user_id_from_target_ref(target_ref: str) -> str | None:
    normalized = target_ref.strip()
    if ":" not in normalized:
        return None
    scheme, value = normalized.split(":", 1)
    if scheme != "user":
        return None
    value = value.strip()
    return value or None


def _document_library_live_check(document_exists: Callable[[str], bool] | None) -> PreflightCheck:
    if document_exists is None:
        return PreflightCheck(
            "documents.live",
            False,
            "Document library live check requested but no adapter check was supplied.",
        )
    try:
        document_exists("work-items/index.md")
    except Exception as exc:  # pragma: no cover - exercised with real Graph failures
        return PreflightCheck("documents.live", False, "Document library live check failed.", str(exc))
    return PreflightCheck("documents.live", True, "Document library accepted a live existence probe.")


def _required_env_check(env: dict[str, str], name: str, check_id: str) -> PreflightCheck:
    value = env.get(name)
    if not value:
        return PreflightCheck(check_id, False, f"{name} is required.")
    return PreflightCheck(check_id, True, f"{name} is present.", "configured")


def _required_scope_check(
    env: dict[str, str],
    name: str,
    check_id: str,
    *,
    any_of: set[str],
    purpose: str,
) -> PreflightCheck:
    value = env.get(name)
    if not value:
        return PreflightCheck(check_id, False, f"{name} is required before checking scopes.")
    try:
        scopes = _jwt_scopes(value)
    except ValueError as exc:
        return PreflightCheck(check_id, False, f"{name} scopes could not be inspected.", str(exc))
    if scopes.intersection(any_of):
        return PreflightCheck(
            check_id,
            True,
            f"{name} has delegated Graph scope for {purpose}.",
            ", ".join(sorted(scopes.intersection(any_of))),
        )
    return PreflightCheck(
        check_id,
        False,
        f"{name} needs delegated Graph scope to {purpose}.",
        f"requires one of: {', '.join(sorted(any_of))}; token has: {', '.join(sorted(scopes)) or 'none'}",
    )


def _jwt_scopes(token: str) -> set[str]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("token is not a JWT")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("JWT payload could not be decoded") from exc
    scopes = claims.get("scp")
    if isinstance(scopes, str):
        return set(scopes.split())
    roles = claims.get("roles")
    if isinstance(roles, list):
        return {str(role) for role in roles}
    return set()


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
