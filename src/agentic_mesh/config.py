from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentic_mesh.models import (
    AuthBinding,
    AuthCredential,
    AuthMethod,
    ConnectorChannelConfig,
    ConnectorIngressConfig,
    DocumentAccountability,
    DocumentLibraryConfig,
    FlowConsult,
    FlowHandoff,
    FlowGate,
    FlowState,
    FlowVisualizationConfig,
    MeshConfig,
    NamingDefaults,
    OrganizationConfig,
    ProjectConfig,
    ProjectConnectorConfig,
    ProjectGoalConfig,
    ProjectMeshConfig,
    ProjectRepositoryConfig,
    ProjectRoleOverride,
    ProjectWorkspaceConfig,
    ResponseTypeTemplate,
    RoleMemoryConfig,
    RoleInstanceConfig,
    RoleTemplate,
    SdlcFlow,
    SponsorInitiatedWorkPolicy,
    TeamsRoleBotConfig,
    WorkerConfig,
)


class ConfigError(ValueError):
    pass


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Expected YAML mapping in {path}")
    return data


def _string_list(data: dict[str, Any], key: str, path: Path) -> list[str]:
    value = data.get(key, []) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{path} {key} must be a list of strings")
    return [str(item) for item in value]


def _decision_rights_from_dict(data: dict[str, Any], path: Path) -> dict[str, list[str]]:
    value = data.get("decision_rights", {}) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path} decision_rights must be a mapping")
    rights: dict[str, list[str]] = {}
    for key, items in value.items():
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise ConfigError(
                f"{path} decision_rights.{key} must be a list of strings"
            )
        rights[str(key)] = [str(item) for item in items]
    return rights


def _core_workflows_from_dict(data: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    value = data.get("core_workflows", []) or []
    if not isinstance(value, list):
        raise ConfigError(f"{path} core_workflows must be a list")
    workflows: list[dict[str, Any]] = []
    for index, workflow in enumerate(value):
        if not isinstance(workflow, dict):
            raise ConfigError(f"{path} core_workflows[{index}] must be a mapping")
        missing = [
            field
            for field in ["workflow_id", "trigger", "outputs"]
            if field not in workflow
        ]
        if missing:
            raise ConfigError(
                f"{path} core_workflows[{index}] is missing required fields: "
                f"{', '.join(missing)}"
            )
        normalized = dict(workflow)
        normalized["workflow_id"] = str(normalized["workflow_id"])
        normalized["trigger"] = str(normalized["trigger"])
        for key in ["inputs", "outputs", "artifacts"]:
            items = normalized.get(key, []) or []
            if not isinstance(items, list) or not all(
                isinstance(item, str) for item in items
            ):
                raise ConfigError(
                    f"{path} core_workflows[{index}].{key} must be a list of strings"
                )
            normalized[key] = [str(item) for item in items]
        workflows.append(normalized)
    return workflows


def _standards_references_from_dict(
    data: dict[str, Any],
    path: Path,
) -> list[dict[str, str]]:
    value = data.get("standards_references", []) or []
    if not isinstance(value, list):
        raise ConfigError(f"{path} standards_references must be a list")
    references: list[dict[str, str]] = []
    for index, reference in enumerate(value):
        if not isinstance(reference, dict):
            raise ConfigError(f"{path} standards_references[{index}] must be a mapping")
        missing = [field for field in ["name", "applies_to"] if field not in reference]
        if missing:
            raise ConfigError(
                f"{path} standards_references[{index}] is missing required fields: "
                f"{', '.join(missing)}"
            )
        references.append({str(key): str(value) for key, value in reference.items()})
    return references


def _role_template_from_dict(data: dict[str, Any], path: Path) -> RoleTemplate:
    required = [
        "role_id",
        "version",
        "purpose",
        "standing_instructions",
        "default_tools",
        "documentation_obligations",
        "handoff_targets",
    ]
    missing = [field for field in required if field not in data]
    if missing:
        raise ConfigError(f"{path} is missing required fields: {', '.join(missing)}")
    return RoleTemplate(
        role_id=str(data["role_id"]),
        version=int(data["version"]),
        purpose=str(data["purpose"]),
        standing_instructions=_string_list(data, "standing_instructions", path),
        default_tools=_string_list(data, "default_tools", path),
        documentation_obligations=_string_list(
            data,
            "documentation_obligations",
            path,
        ),
        handoff_targets=_string_list(data, "handoff_targets", path),
        role_profile=str(data.get("role_profile", "")),
        accountabilities=_string_list(data, "accountabilities", path),
        decision_rights=_decision_rights_from_dict(data, path),
        boundaries=_string_list(data, "boundaries", path),
        collaboration_style=_string_list(data, "collaboration_style", path),
        quality_bar=_string_list(data, "quality_bar", path),
        memory_focus=_string_list(data, "memory_focus", path),
        core_workflows=_core_workflows_from_dict(data, path),
        standards_references=_standards_references_from_dict(data, path),
        anti_patterns=_string_list(data, "anti_patterns", path),
    )


def _organization_from_dict(data: dict[str, Any], path: Path) -> OrganizationConfig:
    required = [
        "organization_id",
        "name",
        "global_language",
        "global_locale",
    ]
    missing = [field for field in required if field not in data]
    if missing:
        raise ConfigError(f"{path} is missing required fields: {', '.join(missing)}")
    naming_data = data.get("naming_defaults", {}) or {}
    if not isinstance(naming_data, dict):
        raise ConfigError(f"{path} naming_defaults must be a mapping")
    naming_defaults = NamingDefaults(
        brand_prefix=str(naming_data.get("brand_prefix", "AM")),
        service_name_template=str(
            naming_data.get(
                "service_name_template",
                "{brand_prefix}.{team_slug}.{role_id}.{ordinal}",
            )
        ),
        bot_display_name_template=str(
            naming_data.get(
                "bot_display_name_template",
                "{brand_prefix}-{role_display_name}",
            )
        ),
        resource_namespace=str(
            naming_data.get("resource_namespace", "agentic-mesh")
        ),
    )
    return OrganizationConfig(
        organization_id=str(data["organization_id"]),
        name=str(data["name"]),
        global_language=str(data["global_language"]),
        global_locale=str(data["global_locale"]),
        naming_defaults=naming_defaults,
        documentation_defaults=dict(data.get("documentation_defaults", {})),
        conversation_defaults=dict(data.get("conversation_defaults", {})),
        work_intake_defaults=dict(data.get("work_intake_defaults", {})),
        handoff_defaults=dict(data.get("handoff_defaults", {})),
        security_defaults=dict(data.get("security_defaults", {})),
    )


def slug_value(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def role_display_name(role_id: str) -> str:
    parts = []
    for part in role_id.split("-"):
        if part.lower() in {"ai", "api", "qa", "ui", "ux"}:
            parts.append(part.upper())
        else:
            parts.append(part.capitalize())
    return " ".join(parts)


def project_team_slug(project: ProjectConfig) -> str:
    preferred = project.connectors.get("teams")
    if preferred and preferred.team_name:
        return slug_value(preferred.team_name)
    for connector in project.connectors.values():
        if connector.team_name:
            return slug_value(connector.team_name)
    return slug_value(project.project_id)


def render_service_name(
    naming: NamingDefaults,
    *,
    team_slug: str,
    project_id: str,
    role_id: str,
    ordinal: int,
) -> str:
    return naming.service_name_template.format(
        brand_prefix=naming.brand_prefix,
        team_slug=team_slug,
        project_id=project_id,
        role_id=role_id,
        role_display_name=role_display_name(role_id),
        ordinal=ordinal,
    )


def render_bot_display_name(naming: NamingDefaults, role_id: str) -> str:
    return naming.bot_display_name_template.format(
        brand_prefix=naming.brand_prefix,
        role_id=role_id,
        role_display_name=role_display_name(role_id),
    )


def _auth_methods_from_dict(data: dict[str, Any], path: Path) -> dict[str, AuthMethod]:
    methods_data = data.get("methods")
    if not isinstance(methods_data, dict) or not methods_data:
        raise ConfigError(f"{path} must declare at least one auth method")

    methods: dict[str, AuthMethod] = {}
    for method_id, method_data in methods_data.items():
        if not isinstance(method_data, dict):
            raise ConfigError(f"Auth method {method_id} must be a mapping")
        methods[str(method_id)] = AuthMethod(
            method_id=str(method_id),
            category=str(method_data["category"]),
            applies_to=list(method_data.get("applies_to", [])),
            env_vars=list(method_data.get("env_vars", [])),
            requires_secret_ref=bool(method_data.get("requires_secret_ref", False)),
            requires_mount_ref=bool(method_data.get("requires_mount_ref", False)),
            description=str(method_data.get("description", "")),
        )
    return methods


def _validate_auth_binding_fields(
    *,
    owner: str,
    adapter: str,
    binding: AuthBinding,
    auth_methods: dict[str, AuthMethod],
) -> None:
    if binding.method not in auth_methods:
        raise ConfigError(f"{owner} references unknown auth method {binding.method}")

    method = auth_methods[binding.method]
    if adapter not in method.applies_to:
        raise ConfigError(
            f"{owner} adapter {adapter} cannot use auth method {binding.method}"
        )
    if method.requires_secret_ref and not binding.secret_ref:
        raise ConfigError(f"{owner} auth method {binding.method} requires secret_ref")
    if method.requires_mount_ref and not binding.mount_ref:
        raise ConfigError(f"{owner} auth method {binding.method} requires mount_ref")


def _auth_credentials_from_dict(
    data: dict[str, Any],
    auth_methods: dict[str, AuthMethod],
) -> dict[str, AuthCredential]:
    credentials_data = data.get("auth_credentials", {}) or {}
    if not isinstance(credentials_data, dict):
        raise ConfigError("Project auth_credentials must be a mapping")

    credentials: dict[str, AuthCredential] = {}
    for credential_id, credential_data in credentials_data.items():
        if not isinstance(credential_data, dict):
            raise ConfigError(f"Auth credential {credential_id} must be a mapping")
        method_id = str(credential_data.get("method", ""))
        if not method_id:
            raise ConfigError(f"Auth credential {credential_id} must declare method")
        if method_id not in auth_methods:
            raise ConfigError(
                f"Auth credential {credential_id} references unknown auth method {method_id}"
            )
        method = auth_methods[method_id]
        credential = AuthCredential(
            credential_id=str(credential_id),
            method=method_id,
            secret_ref=credential_data.get("secret_ref"),
            mount_ref=credential_data.get("mount_ref"),
            env=dict(credential_data.get("env", {})),
            notes=credential_data.get("notes"),
        )
        if method.requires_secret_ref and not credential.secret_ref:
            raise ConfigError(
                f"Auth credential {credential_id} method {method_id} requires secret_ref"
            )
        if method.requires_mount_ref and not credential.mount_ref:
            raise ConfigError(
                f"Auth credential {credential_id} method {method_id} requires mount_ref"
            )
        credentials[str(credential_id)] = credential
    return credentials


def _response_types_from_dict(
    data: dict[str, Any],
    path: Path,
) -> dict[str, ResponseTypeTemplate]:
    response_types_data = data.get("response_types")
    if not isinstance(response_types_data, dict) or not response_types_data:
        raise ConfigError(f"{path} must declare at least one response type")

    response_types: dict[str, ResponseTypeTemplate] = {}
    for response_type_id, response_type_data in response_types_data.items():
        if not isinstance(response_type_data, dict):
            raise ConfigError(f"Response type {response_type_id} must be a mapping")
        required = ["label", "description", "input_mode", "value_type"]
        missing = [
            field
            for field in required
            if field not in response_type_data
        ]
        if missing:
            raise ConfigError(
                f"Response type {response_type_id} is missing required fields: "
                f"{', '.join(missing)}"
            )
        response_types[str(response_type_id)] = ResponseTypeTemplate(
            response_type_id=str(response_type_id),
            label=str(response_type_data["label"]),
            description=str(response_type_data["description"]),
            input_mode=str(response_type_data["input_mode"]),
            value_type=str(response_type_data["value_type"]),
            options=list(response_type_data.get("options", [])),
            validation=dict(response_type_data.get("validation", {})),
            ui_hints=dict(response_type_data.get("ui_hints", {})),
        )
    return response_types


def _auth_binding_from_dict(
    role_id: str,
    adapter: str,
    data: dict[str, Any] | None,
    auth_methods: dict[str, AuthMethod],
    auth_credentials: dict[str, AuthCredential],
) -> AuthBinding | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"Role {role_id} worker auth config must be a mapping")

    credential_ref = data.get("credential") or data.get("credential_ref")
    if credential_ref:
        credential_id = str(credential_ref)
        if credential_id not in auth_credentials:
            raise ConfigError(
                f"Role {role_id} references unknown auth credential {credential_id}"
            )
        credential = auth_credentials[credential_id]
        env = dict(credential.env)
        env.update(dict(data.get("env", {})))
        binding = AuthBinding(
            method=credential.method,
            secret_ref=credential.secret_ref,
            mount_ref=credential.mount_ref,
            env=env,
            notes=data.get("notes") or credential.notes,
            credential_ref=credential_id,
        )
        _validate_auth_binding_fields(
            owner=f"Role {role_id}",
            adapter=adapter,
            binding=binding,
            auth_methods=auth_methods,
        )
        return binding

    method_id = str(data.get("method", ""))
    if not method_id:
        raise ConfigError(
            f"Role {role_id} worker auth config must declare method or credential"
        )

    binding = AuthBinding(
        method=method_id,
        secret_ref=data.get("secret_ref"),
        mount_ref=data.get("mount_ref"),
        env=dict(data.get("env", {})),
        notes=data.get("notes"),
    )
    _validate_auth_binding_fields(
        owner=f"Role {role_id}",
        adapter=adapter,
        binding=binding,
        auth_methods=auth_methods,
    )
    return binding


def _project_role_from_dict(
    role_id: str,
    data: dict[str, Any],
    auth_methods: dict[str, AuthMethod],
    auth_credentials: dict[str, AuthCredential],
) -> ProjectRoleOverride:
    worker = data.get("worker") or {}
    if not isinstance(worker, dict):
        raise ConfigError(f"Role {role_id} worker config must be a mapping")
    instances = int(data.get("instances", 1))
    if instances < 1:
        raise ConfigError(f"Role {role_id} must declare at least one instance")
    adapter = str(worker.get("adapter", "stub"))
    reasoning_effort = str(worker.get("reasoning_effort", "medium"))
    allowed_reasoning_efforts = {"none", "minimal", "low", "medium", "high", "xhigh"}
    if reasoning_effort not in allowed_reasoning_efforts:
        raise ConfigError(
            f"Role {role_id} worker reasoning_effort must be one of: "
            f"{', '.join(sorted(allowed_reasoning_efforts))}"
        )
    sandbox_mode = str(worker.get("sandbox_mode", "workspace-write"))
    allowed_sandbox_modes = {"read-only", "workspace-write", "danger-full-access"}
    if sandbox_mode not in allowed_sandbox_modes:
        raise ConfigError(
            f"Role {role_id} worker sandbox_mode must be one of: "
            f"{', '.join(sorted(allowed_sandbox_modes))}"
        )
    return ProjectRoleOverride(
        role_id=role_id,
        template=str(data.get("template", role_id)),
        instances=instances,
        worker=WorkerConfig(
            adapter=adapter,
            model=str(worker.get("model", "stub")),
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            auth=_auth_binding_from_dict(
                role_id,
                adapter,
                worker.get("auth"),
                auth_methods,
                auth_credentials,
            ),
        ),
        instructions=list(data.get("instructions", [])),
        write_paths=list(data.get("write_paths", [])),
        channels=dict(data.get("channels", {})),
    )


def _project_workspace_from_dict(data: dict[str, Any], project_id: str) -> ProjectWorkspaceConfig:
    workspace_data = data.get("workspace") or {}
    if not isinstance(workspace_data, dict):
        raise ConfigError("Project workspace must be a mapping")

    root = str(workspace_data.get("root", "."))
    repositories_data = workspace_data.get("repositories") or {
        project_id: {
            "type": "git",
            "path": ".",
        }
    }
    if not isinstance(repositories_data, dict) or not repositories_data:
        raise ConfigError("Project workspace.repositories must declare at least one repository")

    repositories: dict[str, ProjectRepositoryConfig] = {}
    for repository_id, repository_data in repositories_data.items():
        if not isinstance(repository_data, dict):
            raise ConfigError(
                f"Project workspace repository {repository_id} must be a mapping"
            )
        repositories[str(repository_id)] = ProjectRepositoryConfig(
            repository_id=str(repository_id),
            type=str(repository_data.get("type", "git")),
            path=str(repository_data.get("path", ".")),
            default_branch=(
                str(repository_data["default_branch"])
                if repository_data.get("default_branch") is not None
                else None
            ),
            remote=(
                str(repository_data["remote"])
                if repository_data.get("remote") is not None
                else None
            ),
        )

    default_repository = str(
        workspace_data.get("default_repository", next(iter(repositories)))
    )
    if default_repository not in repositories:
        raise ConfigError(
            f"Project workspace default_repository {default_repository} is not declared"
        )

    return ProjectWorkspaceConfig(
        root=root,
        default_repository=default_repository,
        repositories=repositories,
    )


def _document_library_from_dict(data: dict[str, Any]) -> DocumentLibraryConfig:
    library_data = data.get("document_library", {}) or {}
    if not isinstance(library_data, dict):
        raise ConfigError("Project document_library must be a mapping")

    backend = str(library_data.get("backend", "filesystem"))
    allowed_backends = {"git", "filesystem", "onedrive", "sharepoint"}
    if backend not in allowed_backends:
        raise ConfigError(
            "Project document_library.backend must be one of: "
            f"{', '.join(sorted(allowed_backends))}"
        )

    return DocumentLibraryConfig(
        backend=backend,
        root=str(library_data.get("root", "docs")),
        structure_policy=str(library_data.get("structure_policy", "togaf-sdlc-v1")),
        index_path=str(
            library_data.get("index_path", "00-index/document-library-manifest.json")
        ),
        review_log_standard=str(
            library_data.get(
                "review_log_standard",
                "same-document-review-log-v1",
            )
        ),
        versioning=str(library_data.get("versioning", "backend")),
        retention_policy=(
            str(library_data["retention_policy"])
            if library_data.get("retention_policy") is not None
            else None
        ),
    )


def _role_memory_from_dict(data: dict[str, Any]) -> RoleMemoryConfig:
    memory_data = data.get("role_memory", {}) or {}
    if not isinstance(memory_data, dict):
        raise ConfigError("Project role_memory must be a mapping")

    backend = str(memory_data.get("backend", "filesystem"))
    allowed_backends = {"filesystem", "git"}
    if backend not in allowed_backends:
        raise ConfigError(
            "Project role_memory.backend must be one of: "
            f"{', '.join(sorted(allowed_backends))}"
        )

    return RoleMemoryConfig(
        enabled=bool(memory_data.get("enabled", True)),
        backend=backend,
        root=str(memory_data.get("root", "memory/roles")),
        provenance_required=bool(memory_data.get("provenance_required", True)),
        refresh_from_document_library=bool(
            memory_data.get("refresh_from_document_library", True)
        ),
        team_overlay_root=str(
            memory_data.get("team_overlay_root", "memory/team-overlays")
        ),
    )


def _project_goal_from_dict(data: dict[str, Any]) -> ProjectGoalConfig:
    goal_data = data.get("goal", {}) or {}
    if not isinstance(goal_data, dict):
        raise ConfigError("Project goal must be a mapping")
    for key in ("success_measures", "constraints", "guidance"):
        value = goal_data.get(key, []) or []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"Project goal.{key} must be a list of strings")
    return ProjectGoalConfig(
        description=str(goal_data.get("description", goal_data.get("outcome", ""))),
        success_measures=[
            str(item) for item in goal_data.get("success_measures", []) or []
        ],
        constraints=[str(item) for item in goal_data.get("constraints", []) or []],
        guidance=[str(item) for item in goal_data.get("guidance", []) or []],
    )


def _project_meshes_from_dict(
    data: dict[str, Any],
    roles: dict[str, ProjectRoleOverride],
) -> dict[str, ProjectMeshConfig]:
    meshes_data = data.get("meshes", {}) or {}
    if not isinstance(meshes_data, dict):
        raise ConfigError("Project meshes must be a mapping")

    meshes: dict[str, ProjectMeshConfig] = {}
    for mesh_id, mesh_data in meshes_data.items():
        if not isinstance(mesh_data, dict):
            raise ConfigError(f"Project mesh {mesh_id} must be a mapping")
        mesh_roles = [str(role_id) for role_id in mesh_data.get("roles", [])]
        if not mesh_roles:
            raise ConfigError(f"Project mesh {mesh_id} must declare roles")
        for role_id in mesh_roles:
            if role_id not in roles:
                raise ConfigError(
                    f"Project mesh {mesh_id} references unconfigured role {role_id}"
                )
        parent_mesh = mesh_data.get("parent_mesh")
        meshes[str(mesh_id)] = ProjectMeshConfig(
            mesh_id=str(mesh_id),
            name=str(mesh_data.get("name", mesh_id)),
            flow=str(mesh_data.get("flow", "sdlc")),
            roles=mesh_roles,
            parent_mesh=str(parent_mesh) if parent_mesh is not None else None,
            purpose=(
                str(mesh_data["purpose"])
                if mesh_data.get("purpose") is not None
                else None
            ),
        )

    for mesh in meshes.values():
        if mesh.parent_mesh is not None and mesh.parent_mesh not in meshes:
            raise ConfigError(
                f"Project mesh {mesh.mesh_id} parent_mesh {mesh.parent_mesh} is not declared"
            )
    return meshes


def _project_connectors_from_dict(
    data: dict[str, Any],
    roles: dict[str, ProjectRoleOverride],
) -> dict[str, ProjectConnectorConfig]:
    connectors_data = data.get("connectors", {}) or {}
    if not isinstance(connectors_data, dict):
        raise ConfigError("Project connectors must be a mapping")

    connectors: dict[str, ProjectConnectorConfig] = {}
    for connector_id, connector_data in connectors_data.items():
        if not isinstance(connector_data, dict):
            raise ConfigError(f"Connector {connector_id} must be a mapping")

        team_data = connector_data.get("team") or {}
        if not isinstance(team_data, dict):
            raise ConfigError(f"Connector {connector_id} team must be a mapping")

        channels_data = connector_data.get("channels") or {}
        if not isinstance(channels_data, dict) or not channels_data:
            raise ConfigError(f"Connector {connector_id} must declare channels")
        channels: dict[str, ConnectorChannelConfig] = {}
        for channel_key, channel_data in channels_data.items():
            if not isinstance(channel_data, dict):
                raise ConfigError(
                    f"Connector {connector_id} channel {channel_key} must be a mapping"
                )
            channels[str(channel_key)] = ConnectorChannelConfig(
                channel_id=str(channel_data["id"]),
                name=str(channel_data.get("name", channel_key)),
            )

        identity_model = str(connector_data.get("identity_model", "shared_bot"))
        role_bots_data = connector_data.get("role_bots", {}) or {}
        if not isinstance(role_bots_data, dict):
            raise ConfigError(f"Connector {connector_id} role_bots must be a mapping")

        role_bots: dict[str, TeamsRoleBotConfig] = {}
        for role_id, bot_data in role_bots_data.items():
            if role_id not in roles:
                raise ConfigError(
                    f"Connector {connector_id} declares role bot for unknown role {role_id}"
                )
            if not isinstance(bot_data, dict):
                raise ConfigError(
                    f"Connector {connector_id} role bot {role_id} must be a mapping"
                )
            role_bots[str(role_id)] = TeamsRoleBotConfig(
                role_id=str(role_id),
                display_name=str(bot_data["display_name"]),
                bot_id_ref=str(bot_data["bot_id_ref"]),
                secret_ref=str(bot_data["secret_ref"]),
            )

        if identity_model == "role_bots":
            missing = sorted(set(roles) - set(role_bots))
            if missing:
                raise ConfigError(
                    f"Connector {connector_id} identity_model role_bots is missing "
                    f"role bot config for: {', '.join(missing)}"
                )

        ingress_data = connector_data.get("ingress")
        ingress = None
        if ingress_data is not None:
            if not isinstance(ingress_data, dict):
                raise ConfigError(f"Connector {connector_id} ingress must be a mapping")
            ingress = ConnectorIngressConfig(
                public_endpoint=str(ingress_data["public_endpoint"]),
                listen_host=str(ingress_data.get("listen_host", "0.0.0.0")),
                listen_port=int(ingress_data.get("listen_port", 3978)),
                path=str(ingress_data.get("path", "/api/messages")),
            )

        connectors[str(connector_id)] = ProjectConnectorConfig(
            connector_id=str(connector_id),
            adapter=str(connector_data["adapter"]),
            identity_model=identity_model,
            tenant_id=(
                str(connector_data["tenant_id"])
                if connector_data.get("tenant_id") is not None
                else None
            ),
            team_id=str(team_data["id"]),
            team_name=str(team_data.get("name", "")),
            channels=channels,
            role_bots=role_bots,
            ingress=ingress,
        )

    return connectors


def _document_accountabilities_from_dict(
    data: dict[str, Any],
    roles: dict[str, ProjectRoleOverride],
) -> dict[str, DocumentAccountability]:
    docs_data = data.get("document_accountabilities", {}) or {}
    if not isinstance(docs_data, dict):
        raise ConfigError("Project document_accountabilities must be a mapping")

    documents: dict[str, DocumentAccountability] = {}
    for path, doc_data in docs_data.items():
        if not isinstance(doc_data, dict):
            raise ConfigError(f"Document accountability {path} must be a mapping")
        owner_role = str(doc_data["owner_role"])
        if owner_role not in roles:
            raise ConfigError(
                f"Document accountability {path} owner_role {owner_role} is not configured"
            )
        contributing_roles = list(doc_data.get("contributing_roles", []))
        for role_id in contributing_roles:
            if role_id not in roles:
                raise ConfigError(
                    f"Document accountability {path} contributor {role_id} is not configured"
                )
        documents[str(path)] = DocumentAccountability(
            path=str(path),
            owner_role=owner_role,
            accountability=str(doc_data.get("accountability", "accountable_owner")),
            can_edit_contributions=bool(doc_data.get("can_edit_contributions", True)),
            review_on_contribution=bool(doc_data.get("review_on_contribution", False)),
            required_sections=list(doc_data.get("required_sections", [])),
            contributing_roles=contributing_roles,
            lifecycle_events=list(doc_data.get("lifecycle_events", [])),
        )
    return documents


def _flow_gate_from_dict(
    state_id: str,
    gate_data: dict[str, Any],
    roles: dict[str, ProjectRoleOverride],
    documents: dict[str, DocumentAccountability],
    response_types: dict[str, ResponseTypeTemplate],
) -> FlowGate:
    gate_id = str(gate_data["gate_id"])
    gate_type = str(gate_data["type"])
    required_documents = list(gate_data.get("required_documents", []))
    for path in required_documents:
        if path not in documents:
            raise ConfigError(
                f"Flow state {state_id} gate {gate_id} references unknown document {path}"
            )
    reviewer_role = gate_data.get("reviewer_role")
    if reviewer_role is not None and reviewer_role not in roles:
        raise ConfigError(
            f"Flow state {state_id} gate {gate_id} reviewer_role {reviewer_role} is not configured"
        )
    response_type = gate_data.get("response_type")
    if gate_type == "human_response":
        if not response_type:
            raise ConfigError(
                f"Flow state {state_id} gate {gate_id} must declare response_type"
            )
        if str(response_type) not in response_types:
            raise ConfigError(
                f"Flow state {state_id} gate {gate_id} references unknown "
                f"response_type {response_type}"
            )
    affected_roles = [str(role_id) for role_id in gate_data.get("affected_roles", [])]
    for role_id in affected_roles:
        if role_id not in roles:
            raise ConfigError(
                f"Flow state {state_id} gate {gate_id} affected_role {role_id} is not configured"
            )
    return FlowGate(
        gate_id=gate_id,
        type=gate_type,
        required_documents=required_documents,
        required_review_status=gate_data.get("required_review_status"),
        reviewer_role=reviewer_role,
        affected_roles=affected_roles,
        review_outcomes=list(gate_data.get("review_outcomes", [])),
        max_resolution_loops=(
            int(gate_data["max_resolution_loops"])
            if gate_data.get("max_resolution_loops") is not None
            else None
        ),
        response_type=response_type,
        prompt=gate_data.get("prompt"),
        requested_from=gate_data.get("requested_from"),
        channel=gate_data.get("channel"),
        timeout=gate_data.get("timeout"),
        on_timeout=gate_data.get("on_timeout"),
        completion_criteria=dict(gate_data.get("completion_criteria", {})),
    )


def _flow_from_dict(
    data: dict[str, Any],
    roles: dict[str, ProjectRoleOverride],
    documents: dict[str, DocumentAccountability],
    response_types: dict[str, ResponseTypeTemplate],
    root: Path,
) -> SdlcFlow:
    flow_data = data.get("flow")
    if not isinstance(flow_data, dict):
        raise ConfigError("Project config must declare a flow overlay")
    flow_data = _resolve_flow_template(root, flow_data)

    states_data = flow_data.get("states")
    if not isinstance(states_data, dict) or not states_data:
        raise ConfigError("Project flow must declare at least one state")

    states: dict[str, FlowState] = {}
    for state_id, state_data in states_data.items():
        if not isinstance(state_data, dict):
            raise ConfigError(f"Flow state {state_id} must be a mapping")
        owner_role = str(state_data.get("owner_role", ""))
        if owner_role not in roles:
            raise ConfigError(
                f"Flow state {state_id} owner_role {owner_role} is not configured"
            )

        handoff_data = state_data.get("handoffs", {}) or {}
        if not isinstance(handoff_data, dict):
            raise ConfigError(f"Flow state {state_id} handoffs must be a mapping")

        handoffs: dict[str, FlowHandoff] = {}
        for status, target_data in handoff_data.items():
            if not isinstance(target_data, dict):
                raise ConfigError(f"Flow state {state_id} handoff {status} must be a mapping")
            target_state = str(target_data["target_state"])
            target_role = str(target_data["target_role"])
            if target_role not in roles:
                raise ConfigError(
                    f"Flow state {state_id} handoff {status} targets unconfigured role {target_role}"
                )
            handoffs[str(status)] = FlowHandoff(
                status=str(status),
                target_state=target_state,
                target_role=target_role,
                message_type=str(target_data.get("message_type", f"sdlc.{target_state}")),
                target_mesh=(
                    str(target_data["target_mesh"])
                    if target_data.get("target_mesh") is not None
                    else None
                ),
                create_work_item=bool(target_data.get("create_work_item", False)),
            )

        consult_data = state_data.get("consults", {}) or {}
        if not isinstance(consult_data, dict):
            raise ConfigError(f"Flow state {state_id} consults must be a mapping")
        consults: dict[str, FlowConsult] = {}
        for consult_id, target_data in consult_data.items():
            if not isinstance(target_data, dict):
                raise ConfigError(
                    f"Flow state {state_id} consult {consult_id} must be a mapping"
                )
            target_state = str(target_data["target_state"])
            target_role = str(target_data["target_role"])
            if target_role not in roles:
                raise ConfigError(
                    f"Flow state {state_id} consult {consult_id} targets unconfigured role {target_role}"
                )
            consults[str(consult_id)] = FlowConsult(
                consult_id=str(consult_id),
                target_state=target_state,
                target_role=target_role,
                message_type=str(
                    target_data.get("message_type", f"sdlc.consult.{target_state}")
                ),
                purpose=str(target_data.get("purpose", "")),
            )

        gates_data = state_data.get("gates", []) or []
        if not isinstance(gates_data, list):
            raise ConfigError(f"Flow state {state_id} gates must be a list")
        gates = [
            _flow_gate_from_dict(str(state_id), gate, roles, documents, response_types)
            for gate in gates_data
        ]

        states[str(state_id)] = FlowState(
            state_id=str(state_id),
            owner_role=owner_role,
            purpose=str(state_data.get("purpose", "")),
            artifact_path=str(state_data["artifact_path"]),
            handoffs=handoffs,
            consults=consults,
            gates=gates,
        )

    entry_state = str(flow_data.get("entry_state", ""))
    if entry_state not in states:
        raise ConfigError(f"Project flow entry_state {entry_state} is not declared")

    for state in states.values():
        for handoff in state.handoffs.values():
            if handoff.target_state not in states:
                raise ConfigError(
                    f"Flow state {state.state_id} targets missing state {handoff.target_state}"
                )
        for consult in state.consults.values():
            if consult.target_state not in states:
                raise ConfigError(
                    f"Flow state {state.state_id} consults missing state {consult.target_state}"
                )

    work_item_types = list(flow_data.get("work_item_types", []))
    sponsor_policy = _sponsor_initiated_work_policy_from_dict(
        flow_data.get("sponsor_initiated_work"),
        work_item_types,
        states,
    )

    return SdlcFlow(
        flow_id=str(flow_data.get("flow_id", "default-sdlc")),
        entry_state=entry_state,
        work_item_types=work_item_types,
        states=states,
        sponsor_initiated_work=sponsor_policy,
        visualization=_flow_visualization_from_dict(flow_data.get("visualization")),
    )


def _flow_visualization_from_dict(data: Any) -> FlowVisualizationConfig:
    if data is None:
        return FlowVisualizationConfig()
    if not isinstance(data, dict):
        raise ConfigError("Project flow visualization must be a mapping")
    return FlowVisualizationConfig(
        enabled=bool(data.get("enabled", True)),
        default_format=str(data.get("default_format", "mermaid")),
        group_by=list(data.get("group_by", ["mesh", "role"])),
    )


def _sponsor_initiated_work_policy_from_dict(
    data: Any,
    work_item_types: list[str],
    states: dict[str, FlowState],
) -> SponsorInitiatedWorkPolicy | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError("Project flow sponsor_initiated_work must be a mapping")

    default_work_item_type = str(data.get("default_work_item_type", "spike"))
    if default_work_item_type not in work_item_types:
        raise ConfigError(
            "Project flow sponsor_initiated_work.default_work_item_type "
            f"{default_work_item_type} is not declared in work_item_types"
        )

    default_intake_state = str(data.get("default_intake_state", next(iter(states))))
    if default_intake_state not in states:
        raise ConfigError(
            "Project flow sponsor_initiated_work.default_intake_state "
            f"{default_intake_state} is not declared"
        )

    return SponsorInitiatedWorkPolicy(
        allow_from_any_state=bool(data.get("allow_from_any_state", False)),
        default_work_item_type=default_work_item_type,
        default_intake_state=default_intake_state,
        capture_rule=str(data.get("capture_rule", "")),
        routing_rule=str(data.get("routing_rule", "")),
        completion_rule=str(data.get("completion_rule", "")),
    )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_flow_template(root: Path, flow_data: dict[str, Any]) -> dict[str, Any]:
    template_id = flow_data.get("template")
    if not template_id:
        return flow_data
    template_path = root / "config" / "flows" / f"{template_id}.yaml"
    template_data = _load_yaml(template_path)
    overrides = {
        key: value
        for key, value in flow_data.items()
        if key not in {"template", "overrides"}
    }
    if isinstance(flow_data.get("overrides"), dict):
        overrides = _deep_merge(overrides, flow_data["overrides"])
    return _deep_merge(template_data, overrides)


def _validate_role_channels(
    roles: dict[str, ProjectRoleOverride],
    connectors: dict[str, ProjectConnectorConfig],
) -> None:
    connector_channels = {
        channel
        for connector in connectors.values()
        for channel in connector.channels
    }
    if not connector_channels:
        return
    for role_id, role in roles.items():
        for alias, channel in role.channels.items():
            if channel not in connector_channels:
                raise ConfigError(
                    f"Role {role_id} channel alias {alias} references "
                    f"unknown connector channel {channel}"
                )


def load_mesh_config(
    root: Path,
    project_file: str = "examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
) -> MeshConfig:
    root = root.resolve()
    organization = _organization_from_dict(
        _load_yaml(root / "config" / "organization.yaml"),
        root / "config" / "organization.yaml",
    )
    auth_methods = _auth_methods_from_dict(
        _load_yaml(root / "config" / "auth-methods.yaml"),
        root / "config" / "auth-methods.yaml",
    )
    response_types = _response_types_from_dict(
        _load_yaml(root / "config" / "response-types.yaml"),
        root / "config" / "response-types.yaml",
    )
    role_templates: dict[str, RoleTemplate] = {}
    for path in sorted((root / "config" / "roles").glob("*.yaml")):
        template = _role_template_from_dict(_load_yaml(path), path)
        role_templates[template.role_id] = template

    project_path = Path(project_file)
    if not project_path.is_absolute():
        project_path = root / project_path
    project_data = _load_yaml(project_path)
    roles_data = project_data.get("roles")
    if not isinstance(roles_data, dict) or not roles_data:
        raise ConfigError(f"{project_path} must declare at least one role")

    auth_credentials = _auth_credentials_from_dict(project_data, auth_methods)
    roles = {
        role_id: _project_role_from_dict(
            role_id,
            role_data,
            auth_methods,
            auth_credentials,
        )
        for role_id, role_data in roles_data.items()
    }
    workspace = _project_workspace_from_dict(project_data, str(project_data["project_id"]))
    goal = _project_goal_from_dict(project_data)
    document_library = _document_library_from_dict(project_data)
    role_memory = _role_memory_from_dict(project_data)
    meshes = _project_meshes_from_dict(project_data, roles)
    connectors = _project_connectors_from_dict(project_data, roles)
    _validate_role_channels(roles, connectors)
    document_accountabilities = _document_accountabilities_from_dict(project_data, roles)
    flow = _flow_from_dict(
        project_data,
        roles,
        document_accountabilities,
        response_types,
        root,
    )
    project = ProjectConfig(
        project_id=str(project_data["project_id"]),
        name=str(project_data.get("name", project_data["project_id"])),
        workspace=workspace,
        roles=roles,
        goal=goal,
        auth_credentials=auth_credentials,
        connectors=connectors,
        document_accountabilities=document_accountabilities,
        flow=flow,
        document_library=document_library,
        role_memory=role_memory,
        meshes=meshes,
    )

    instances: dict[str, RoleInstanceConfig] = {}
    team_slug = project_team_slug(project)
    for role_id, override in roles.items():
        if override.template not in role_templates:
            raise ConfigError(
                f"Project role {role_id} references missing template {override.template}"
            )
        template = role_templates[override.template]
        for ordinal in range(1, override.instances + 1):
            instance_id = f"{project.project_id}.{role_id}.{ordinal}"
            instances[instance_id] = RoleInstanceConfig(
                instance_id=instance_id,
                project_id=project.project_id,
                role_id=role_id,
                ordinal=ordinal,
                template=template,
                override=override,
                service_name=render_service_name(
                    organization.naming_defaults,
                    team_slug=team_slug,
                    project_id=project.project_id,
                    role_id=role_id,
                    ordinal=ordinal,
                ),
            )

    return MeshConfig(
        organization=organization,
        auth_methods=auth_methods,
        response_types=response_types,
        project=project,
        role_templates=role_templates,
        instances=instances,
    )
