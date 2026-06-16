from pathlib import Path
import json

import jsonschema
import yaml


def _schema() -> dict[str, object]:
    return json.loads(Path("config/schemas/project-v3.schema.json").read_text(encoding="utf-8"))


def test_project_v3_schema_allows_local_agent_runtime_config() -> None:
    project = {
        "project_id": "agentic-mesh-dev",
        "broker": {
            "adapter": "in-memory",
            "stream": "agent-inbox",
        },
        "document_library": {
            "adapter": "filesystem",
            "root": "documents",
        },
        "flow": {
            "template": "sdlc",
        },
        "connectors": {
            "teams": {
                "adapter": "local",
                "role_bots": {
                    "product-manager": {
                        "display_name": "AM-Product Manager",
                        "bot_id_ref": "teams-bot-product-manager-app-id",
                    }
                },
            }
        },
        "release_deployment_targets": {
            "local-smoke": {
                "type": "command",
                "command": ["python", "-c", "print('deployed')"],
                "timeout_seconds": 30,
                "rollback_summary": "Re-run the previous target.",
            },
            "planning-only": {
                "type": "no_deployment",
                "reason": "Planning-only slice.",
            },
        },
        "roles": {
            "product-manager": {
                "template": "product-manager",
                "instances": 1,
                "worker": {
                    "adapter": "codex-cli",
                    "command": ["codex", "exec"],
                    "timeout_seconds": 900,
                    "model": "codex",
                    "reasoning_effort": "high",
                    "sandbox_mode": "danger-full-access",
                    "auth": {"credential": "codex-agentic-mesh-dev-team-q"},
                },
                "instructions": ["Shape the product."],
                "write_paths": ["docs/product/**"],
                "channels": {"primary": "product", "handoff_inbox": "product"},
            }
        },
    }

    jsonschema.Draft202012Validator(_schema()).validate(project)


def test_project_v3_schema_allows_current_dogfood_project_config() -> None:
    project = yaml.safe_load(
        Path("examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml").read_text(encoding="utf-8")
    )

    jsonschema.Draft202012Validator(_schema()).validate(project)


def test_project_v3_schema_rejects_unknown_teams_adapter() -> None:
    project = {
        "project_id": "agentic-mesh-dev",
        "connectors": {"teams": {"adapter": "unsupported-adapter"}},
        "roles": {"product-manager": {"instances": 1}},
    }

    validator = jsonschema.Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(project), key=lambda item: item.json_path)

    assert errors
    assert "unsupported-adapter" in errors[0].message
