from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_mesh_v3.project_install import AzureCliGraphClient
from agentic_mesh_v3.project_install import InstallOptions
from agentic_mesh_v3.project_install import TokenGraphClient
from agentic_mesh_v3.project_install import run_project_install


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentic-mesh-v3-install-project",
        description="Dependency-light V3 project installer entry point for bootstrap hosts.",
    )
    parser.add_argument("--project-file", type=Path, required=True)
    parser.add_argument("--organization-file", type=Path)
    parser.add_argument("--graph-token-file", type=Path)
    parser.add_argument("--teams-app-package-root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-create-team", action="store_true")
    parser.add_argument("--allow-create-channel", action="store_true")
    parser.add_argument("--allow-register-apps", action="store_true")
    parser.add_argument("--allow-register-bot-services", action="store_true")
    parser.add_argument("--allow-install-apps", action="store_true")
    parser.add_argument("--allow-uninstall-stale", action="store_true")
    parser.add_argument("--allow-secret-rotation", action="store_true")
    args = parser.parse_args(argv)

    graph_client = (
        TokenGraphClient.from_file(args.graph_token_file)
        if args.graph_token_file is not None
        else AzureCliGraphClient()
    )
    result = run_project_install(
        graph_client=graph_client,
        project_file=args.project_file,
        organization_file=args.organization_file,
        options=InstallOptions(
            apply=bool(args.apply),
            allow_create_team=bool(args.allow_create_team),
            allow_create_channel=bool(args.allow_create_channel),
            allow_register_apps=bool(args.allow_register_apps),
            allow_register_bot_services=bool(args.allow_register_bot_services),
            allow_install_apps=bool(args.allow_install_apps),
            allow_uninstall_stale=bool(args.allow_uninstall_stale),
            allow_secret_rotation=bool(args.allow_secret_rotation),
        ),
        teams_app_package_root=args.teams_app_package_root,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"ok", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
