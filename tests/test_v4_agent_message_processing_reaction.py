from __future__ import annotations

from pathlib import Path
from typing import Any

from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.config import V4RoleConfig
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.runtime import V4Runtime
from agentic_mesh_v4.teams_delivery import PROCESSING_REACTION_GLYPH
from agentic_mesh_v4.teams_delivery import PROCESSING_REACTION_NAME
from v4_postgres import make_v4_db


class FakeCodexClient:
    def __init__(self, timeline: list[str]) -> None:
        self.initialized = False
        self.timeline = timeline
        self.events: list[dict[str, Any]] = [
            {"method": "item/agentMessage/delta", "params": {"delta": "Acknowledged."}},
            {"method": "turn/completed", "params": {}},
        ]

    def initialize(self) -> dict[str, Any]:
        self.initialized = True
        self.timeline.append("codex.initialize")
        return {}

    def start_thread(self, **_: Any) -> str:
        self.timeline.append("codex.thread/start")
        return "thread-1"

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        self.timeline.append(f"codex.thread/resume:{thread_id}")
        return {}

    def start_turn(self, **_: Any) -> str:
        self.timeline.append("codex.turn/start")
        return "turn-1"

    def steer_turn(self, **_: Any) -> dict[str, Any]:
        self.timeline.append("codex.turn/steer")
        return {}

    def receive_event(self) -> dict[str, Any] | None:
        if self.events:
            return self.events.pop(0)
        return None

    def respond_to_server_request(self, **_: Any) -> None:
        return None


class FakeTeamsReplySender:
    def __init__(self, timeline: list[str], *, reaction_outcome: str = "delivered") -> None:
        self.timeline = timeline
        self.reaction_outcome = reaction_outcome
        self.reactions: list[dict[str, object]] = []
        self.replies: list[str] = []

    def add_processing_reaction(self, *, role_id: str, activity: dict[str, object]) -> str | None:
        self.timeline.append("teams.processing_reaction")
        if self.reaction_outcome == "failed":
            raise RuntimeError("simulated reaction failure")
        if not activity.get("id"):
            return None
        self.reactions.append(
            {
                "role_id": role_id,
                "activity_id": activity["id"],
                "reaction": {"name": PROCESSING_REACTION_NAME, "glyph": PROCESSING_REACTION_GLYPH},
            }
        )
        return "reaction-1"

    def send_reply(self, *, role_id: str, activity: dict[str, object], text_markdown: str) -> str:
        self.timeline.append("teams.reply")
        self.replies.append(text_markdown)
        return "reply-1"


def test_v4_runtime_adds_processing_reaction_before_turn_start(tmp_path: Path) -> None:
    db = _db(tmp_path)
    timeline: list[str] = []
    sender = FakeTeamsReplySender(timeline)
    runtime = _runtime(db, timeline=timeline, sender=sender)
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="engineering",
        text="Please take this.",
        source="teams",
        payload=_teams_activity(),
    )

    result = runtime.dispatch_once(project_id="agentic-mesh-dev", role_id="engineering")

    assert result is not None
    assert result.state == "completed"
    assert timeline.index("teams.processing_reaction") < timeline.index("codex.turn/start")
    assert sender.reactions == [
        {
            "role_id": "engineering",
            "activity_id": "activity-1",
            "reaction": {"name": "eyes", "glyph": "\U0001F440"},
        }
    ]
    assert ("processing_reaction", "delivered") in _journal(db, message_id)
    assert ("completed", "completed") in _journal(db, message_id)


def test_v4_runtime_skips_processing_reaction_without_activity_reference(tmp_path: Path) -> None:
    db = _db(tmp_path)
    timeline: list[str] = []
    sender = FakeTeamsReplySender(timeline)
    runtime = _runtime(db, timeline=timeline, sender=sender)
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="engineering",
        text="Please take this.",
        source="teams",
        payload=_teams_activity(activity_id=None),
    )

    result = runtime.dispatch_once(project_id="agentic-mesh-dev", role_id="engineering")

    assert result is not None
    assert result.state == "completed"
    assert sender.reactions == []
    assert ("processing_reaction", "unsupported") in _journal(db, message_id)


def test_v4_runtime_continues_when_processing_reaction_fails(tmp_path: Path) -> None:
    db = _db(tmp_path)
    timeline: list[str] = []
    sender = FakeTeamsReplySender(timeline, reaction_outcome="failed")
    runtime = _runtime(db, timeline=timeline, sender=sender)
    runtime.register_roles()
    message_id = runtime.enqueue_conversation(
        target_role="engineering",
        text="Please take this.",
        source="teams",
        payload=_teams_activity(),
    )

    result = runtime.dispatch_once(project_id="agentic-mesh-dev", role_id="engineering")

    assert result is not None
    assert result.state == "completed"
    assert sender.replies == ["Acknowledged."]
    assert ("processing_reaction", "failed") in _journal(db, message_id)
    events = [
        (row["event_type"], row["content"])
        for row in db.connection.execute("SELECT event_type, content FROM agent_events")
    ]
    assert ("processing_reaction/failed", "simulated reaction failure") in events


def test_v4_runtime_adds_processing_reaction_before_turn_steer(tmp_path: Path) -> None:
    db = _db(tmp_path)
    timeline: list[str] = []
    sender = FakeTeamsReplySender(timeline)
    runtime = _runtime(db, timeline=timeline, sender=sender)
    runtime.register_roles()
    db.enqueue_message(
        target_role="engineering",
        text="Already active",
        source="teams",
        conversation_ref="conversation-1",
        payload=_teams_activity(activity_id="activity-active"),
    )
    assert db.claim_next_message(
        role_id="engineering",
        worker_id="agentic-mesh-dev.engineering.1",
    ) is not None
    with db.connection:
        db.connection.execute(
            """
            UPDATE role_instances
            SET active_thread_id='thread-1', state='active'
            WHERE role_instance_id='agentic-mesh-dev.engineering.1'
            """
        )

    message_id = runtime.enqueue_or_steer_conversation(
        target_role="engineering",
        text="Please also consider this.",
        source="teams",
        conversation_ref="conversation-1",
        payload=_teams_activity(activity_id="activity-steer"),
    )

    assert timeline.index("teams.processing_reaction") < timeline.index("codex.turn/steer")
    assert sender.reactions[-1]["activity_id"] == "activity-steer"
    assert ("processing_reaction", "delivered") in _journal(db, message_id)
    row = db.connection.execute(
        "SELECT state FROM message_queue WHERE message_id=?",
        (message_id,),
    ).fetchone()
    assert row["state"] == "steered"


def _db(tmp_path: Path) -> V4Database:
    db = make_v4_db()
    return db


def _runtime(db: V4Database, *, timeline: list[str], sender: FakeTeamsReplySender) -> V4Runtime:
    config = V4ProjectConfig(
        project_id="agentic-mesh-dev",
        name="Agentic Mesh Dev",
        goal="Test",
        roles=(
            V4RoleConfig(
                role_id="engineering",
                display_name="Engineering",
                template="engineering",
                model="gpt-5",
                approval_policy="never",
            ),
        ),
    )
    client = FakeCodexClient(timeline)
    return V4Runtime(
        db=db,
        project_config=config,
        client_factory=lambda role_id: client,
        teams_reply_sender=sender,
    )


def _teams_activity(*, activity_id: str | None = "activity-1") -> dict[str, object]:
    payload: dict[str, object] = {
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "conversation": {"id": "19:thread@thread.tacv2"},
        "text": "Please take this.",
    }
    if activity_id is not None:
        payload["id"] = activity_id
    return payload


def _journal(db: V4Database, message_id: str) -> list[tuple[str, str]]:
    return [
        (str(row["stage"]), str(row["status"]))
        for row in db.connection.execute(
            "SELECT stage, status FROM message_journal WHERE message_id=? ORDER BY created_at",
            (message_id,),
        )
    ]

