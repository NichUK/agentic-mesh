#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys


MANIFEST_PATH = Path("/opt/agentic-mesh/image-manifest.json")
FORBIDDEN_IMAGE_PATHS = (
    Path("/opt/agentic-mesh/prompts"),
    Path("/opt/agentic-mesh/memory"),
    Path("/opt/agentic-mesh/projects"),
    Path("/opt/agentic-mesh/state"),
    Path("/opt/agentic-mesh/credentials"),
)


def main() -> int:
    errors: list[str] = []
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        value = {}
        errors.append(f"invalid image manifest: {exc}")
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        errors.append("unsupported image manifest")
    profile_id = value.get("profile_id") if isinstance(value, dict) else None
    expected_profile = os.environ.get("AGENTIC_MESH_TOOL_PROFILE_ID")
    if not isinstance(profile_id, str) or profile_id != expected_profile:
        errors.append("image profile identity does not match its environment")
    commands = value.get("required_commands") if isinstance(value, dict) else None
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(item, str) and item for item in commands)
        or len(commands) != len(set(commands))
    ):
        errors.append("required_commands must be a non-empty unique string list")
    else:
        missing = sorted(command for command in commands if shutil.which(command) is None)
        if missing:
            errors.append(f"required commands are missing: {missing}")
    present_forbidden = sorted(
        str(path) for path in FORBIDDEN_IMAGE_PATHS if path.exists()
    )
    if present_forbidden:
        errors.append(f"forbidden project or runtime paths are present: {present_forbidden}")
    print(
        json.dumps(
            {
                "status": "healthy" if not errors else "unhealthy",
                "profile_id": profile_id,
                "errors": errors,
            },
            sort_keys=True,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
