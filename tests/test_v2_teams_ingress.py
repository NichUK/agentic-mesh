from pathlib import Path

from agentic_mesh_v2.connectors import LocalTeamsTestAdapter
from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.project_config import load_teams_connector_config
from agentic_mesh_v2.teams_ingress import normalize_bot_activity


PROJECT_FILE = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml")


def test_bot_framework_channel_activity_routes_to_mentioned_role(tmp_path: Path) -> None:
    config = load_teams_connector_config(PROJECT_FILE, external_base_url="http://linuxch:8100")
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()

    event = normalize_bot_activity(
        {
            "type": "message",
            "id": "1781347975945",
            "text": "<at>AM-Product Manager</at> Test to ensure connectivity. Please respond, but do not create work.",
            "from": {
                "id": "29:user",
                "aadObjectId": "sponsor-aad-id",
                "name": "Nicholas Overend",
            },
            "recipient": {
                "id": "28:product-manager-bot",
                "name": "AM-Product Manager",
            },
            "conversation": {
                "id": "19:fS0LN3jkUb5T7hMvueOm-o1PHRoIAk4lm_8MscXSCXE1@thread.tacv2",
                "conversationType": "channel",
            },
            "channelData": {
                "team": {"id": "e664f0d3-2d3f-4ef4-9102-2b99e1601169"},
                "channel": {"id": "19:fS0LN3jkUb5T7hMvueOm-o1PHRoIAk4lm_8MscXSCXE1@thread.tacv2"},
            },
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>AM-Product Manager</at>",
                    "mentioned": {"id": "28:product-manager-bot", "name": "AM-Product Manager"},
                }
            ],
        },
        role_display_names={"product-manager": "AM-Product Manager"},
    )
    replayed = adapter.replay_event(event)

    snapshot = db.status_snapshot()
    assert replayed.route_type == "role_mention"
    assert replayed.mentioned_roles == ("product-manager",)
    assert snapshot["counts"]["role_assignments"] == 1
    assert snapshot["role_assignments"][0]["role_id"] == "product-manager"
    assert snapshot["conversation_events"][0]["body_preview"].startswith("AM-Product Manager Test")


def test_bot_framework_channel_activity_routes_to_recipient_role_without_entities(tmp_path: Path) -> None:
    config = load_teams_connector_config(PROJECT_FILE, external_base_url="http://linuxch:8100")
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()

    event = normalize_bot_activity(
        {
            "type": "message",
            "id": "1781352964382",
            "serviceUrl": "https://smba.trafficmanager.net/uk/tenant-id/",
            "text": "AM-Product Manager Test to ensure connectivity. Please respond, but do not create work.",
            "from": {
                "id": "29:user",
                "aadObjectId": "sponsor-aad-id",
                "name": "Nicholas Overend",
            },
            "recipient": {
                "id": "28:product-manager-bot",
                "name": "AM-Product Manager",
            },
            "conversation": {
                "id": "19:fS0LN3jkUb5T7hMvueOm-o1PHRoIAk4lm_8MscXSCXE1@thread.tacv2",
                "conversationType": "channel",
            },
            "channelData": {
                "team": {"id": "e664f0d3-2d3f-4ef4-9102-2b99e1601169"},
                "channel": {"id": "19:fS0LN3jkUb5T7hMvueOm-o1PHRoIAk4lm_8MscXSCXE1@thread.tacv2"},
            },
        },
        role_display_names={"product-manager": "AM-Product Manager"},
    )
    replayed = adapter.replay_event(event)

    snapshot = db.status_snapshot()
    assert replayed.route_type == "role_mention"
    assert replayed.mentioned_roles == ("product-manager",)
    assert snapshot["counts"]["role_assignments"] == 1
    assignment = snapshot["role_assignments"][0]
    assert assignment["role_id"] == "product-manager"
    assert assignment["payload"]["service_url"] == "https://smba.trafficmanager.net/uk/tenant-id/"
    assert assignment["payload"]["reply_to_id"] == "1781352964382"


def test_bot_framework_personal_activity_routes_to_recipient_role(tmp_path: Path) -> None:
    config = load_teams_connector_config(PROJECT_FILE, external_base_url="http://linuxch:8100")
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    adapter = LocalTeamsTestAdapter(db, config)
    adapter.install()

    event = normalize_bot_activity(
        {
            "type": "message",
            "id": "dm-1",
            "text": "Give me a status update.",
            "from": {"id": "29:user", "name": "Nicholas Overend"},
            "recipient": {"id": "28:product-manager-bot", "name": "AM-Product Manager"},
            "conversation": {"id": "a:dm-conversation", "conversationType": "personal"},
        },
        role_display_names={"product-manager": "AM-Product Manager"},
    )
    replayed = adapter.replay_event(event)

    snapshot = db.status_snapshot()
    assert replayed.route_type == "role_direct_message"
    assert snapshot["counts"]["role_assignments"] == 1
    assert snapshot["role_assignments"][0]["role_id"] == "product-manager"
