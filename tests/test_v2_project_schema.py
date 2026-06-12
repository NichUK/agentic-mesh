from pathlib import Path
import json

import jsonschema


def test_project_schema_allows_command_backed_codex_worker() -> None:
    schema = json.loads(Path("config/schemas/project.schema.json").read_text(encoding="utf-8"))
    project = {
        "project_id": "test-project",
        "name": "Test Project",
        "workspace": {
            "root": "examples/projects/test-project",
            "default_repository": "app",
            "repositories": {
                "app": {
                    "type": "git",
                    "path": ".",
                }
            },
        },
        "flow": {
            "template": "sdlc",
        },
        "container_lifecycle": {
            "adapter": "docker-compose",
            "compose_files": ["deploy/compose/docker-compose.yml"],
            "service_name_template": "{project_id}-{role_id}-{index}",
            "working_directory": "deploy/compose",
        },
        "roles": {
            "engineering": {
                "template": "engineering",
                "instances": 1,
                "worker": {
                    "adapter": "codex-cli",
                    "model": "gpt-test",
                    "reasoning_effort": "high",
                    "sandbox_mode": "workspace-write",
                    "command": ["codex", "exec"],
                    "timeout_seconds": 3600,
                    "auth": {
                        "credential": "codex-test",
                    },
                },
                "instructions": ["Implement assigned work."],
                "write_paths": ["src/**", "tests/**"],
                "channels": {
                    "primary": "engineering",
                },
                "container_lifecycle": {
                    "adapter": "docker-compose",
                    "compose_files": ["deploy/compose/docker-compose.yml"],
                    "service_name_template": "engineering-{index}",
                },
            }
        },
    }

    jsonschema.Draft202012Validator(schema).validate(project)
