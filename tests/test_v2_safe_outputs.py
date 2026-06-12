from pathlib import Path

import pytest

from agentic_mesh_v2.db import V2Database
from agentic_mesh_v2.safe_outputs import SafeOutputCall
from agentic_mesh_v2.safe_outputs import SafeOutputError
from agentic_mesh_v2.safe_outputs import SafeOutputService


def _db(tmp_path: Path) -> V2Database:
    db = V2Database(tmp_path / "v2.sqlite3")
    db.migrate()
    db.create_run(
        run_id="run-1",
        role_id="product-manager",
        role_instance_id="pm-1",
        work_item_id=None,
    )
    return db


def test_role_scoped_tools_allow_specialist_handoff_but_reject_release_power(tmp_path: Path) -> None:
    db = _db(tmp_path)
    service = SafeOutputService(db)

    service.record(
        run_id="run-1",
        call=SafeOutputCall(
            role_id="product-manager",
            tool_name="handoff.request",
            payload={"target_role": "ux-designer", "reason": "UX implications."},
            terminal=True,
        ),
    )

    with pytest.raises(SafeOutputError, match="not authorized"):
        service.record(
            run_id="run-1",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="release.deploy",
                payload={
                    "work_item_id": "work-1",
                    "target_id": "dogfood",
                    "reason": "Ship it.",
                },
            ),
        )


def test_status_reply_cannot_claim_fake_work_creation(tmp_path: Path) -> None:
    db = _db(tmp_path)
    service = SafeOutputService(db)

    for message in [
        "I created work-123 for this.",
        "I superseded the old dashboard work.",
        "I closed the work item after review.",
        "I overrode blocker handling for the old slice.",
        "I overrode the blocker for the old slice.",
    ]:
        with pytest.raises(SafeOutputError, match="must not claim durable mutations"):
            service.record(
                run_id="run-1",
                call=SafeOutputCall(
                    role_id="product-manager",
                    tool_name="status.reply",
                    payload={"message": message},
                    terminal=True,
                ),
            )


def test_status_reply_cannot_claim_connector_delivery_success(tmp_path: Path) -> None:
    db = _db(tmp_path)
    service = SafeOutputService(db)

    with pytest.raises(SafeOutputError, match="connector delivery success"):
        service.record(
            run_id="run-1",
            call=SafeOutputCall(
                role_id="product-manager",
                tool_name="status.reply",
                payload={"message": "I delivered the Teams reply successfully to the human."},
                terminal=True,
            ),
        )
