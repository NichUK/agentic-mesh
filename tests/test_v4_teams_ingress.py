from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v4.config import load_project_config
from agentic_mesh_v4.server import _target_role


PROJECT_CONFIG = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")


def test_v4_teams_dm_routes_by_recipient_bot_name_before_text_role_mentions() -> None:
    config = load_project_config(PROJECT_CONFIG)
    payload = {
        "channelId": "msteams",
        "text": "work-item mentions the product manager but I am asking Project Manager",
        "recipient": {"id": "28:project-manager-app", "name": "AM-Project Manager"},
    }

    assert _target_role(payload, config) == "project-manager"


def test_v4_teams_activity_routes_by_recipient_bot_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_project_config(PROJECT_CONFIG)
    monkeypatch.setenv("TEAMS_BOT_DELIVERY_MANAGER_APP_ID", "delivery-app-id")
    payload = {
        "channelId": "msteams",
        "text": "engineering and product-manager are mentioned in the message body",
        "recipient": {"id": "28:delivery-app-id", "name": ""},
    }

    assert _target_role(payload, config) == "delivery-manager"


def test_v4_non_teams_target_role_can_still_be_explicit() -> None:
    config = load_project_config(PROJECT_CONFIG)
    payload = {
        "text": "ask the project manager about delivery",
        "target_role": "release-manager",
    }

    assert _target_role(payload, config) == "release-manager"
