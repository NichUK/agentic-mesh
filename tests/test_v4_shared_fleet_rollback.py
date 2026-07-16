from __future__ import annotations

import pytest

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.shared_fleet import STAGE1_DISPOSABLE_PERMIT
from agentic_mesh_v4.shared_fleet import discover_postgres_catalog
from agentic_mesh_v4.shared_fleet import apply_disposable_role_metadata
from agentic_mesh_v4.shared_fleet import rollback_disposable_role_metadata

from shared_fleet_support import ROLE_ID
from shared_fleet_support import source
from v4_postgres import make_v4_db


def test_disposable_rollback_restores_legacy_identity_and_business_counts() -> None:
    db = make_v4_db()
    try:
        db.upsert_role_instance(
            role_instance_id="agentic-mesh-dev.engineering.1",
            role_id=ROLE_ID,
            display_name="Engineering",
            service_name="agentic-mesh-dev-engineering-1",
            authority="full",
            codex_endpoint="ws://synthetic:4700",
        )
        db.upsert_work_item(
            work_item_id="synthetic-work",
            title="Synthetic work",
            state="planned",
            owner_role=ROLE_ID,
            next_action="remain",
        )
        before = _business_counts(db)
        apply_disposable_role_metadata(
            db.connection,
            project_id="agentic-mesh-dev",
            sources=(source("agentic-mesh-dev"),),
            control_schema=True,
            permit=STAGE1_DISPOSABLE_PERMIT,
        )
        assert rollback_disposable_role_metadata(
            db.connection,
            permit=STAGE1_DISPOSABLE_PERMIT,
        ) == 2
        row = db.connection.execute(
            "SELECT identity_kind, fleet_instance_id, assigned_project_id FROM role_instances WHERE role_instance_id=?",
            ("agentic-mesh-dev.engineering.1",),
        ).fetchone()
        assert row == {
            "identity_kind": "legacy_project_instance",
            "fleet_instance_id": None,
            "assigned_project_id": None,
        }
        assert _business_counts(db) == before
    finally:
        db.close()


def test_disposable_apply_refuses_non_test_schema() -> None:
    class PublicConnection:
        def execute(self, _sql: str, _params: object = ()) -> object:
            return self

        def fetchone(self) -> dict[str, str]:
            return {"schema": "public"}

    with pytest.raises(PermissionError, match="outside a disposable schema"):
        apply_disposable_role_metadata(
            PublicConnection(),
            project_id="agentic-mesh-dev",
            sources=(),
            control_schema=True,
            permit=STAGE1_DISPOSABLE_PERMIT,
        )


@pytest.mark.parametrize("stop_step", ("schema", "assignments", "physical_identity"))
def test_every_transaction_boundary_rolls_back_without_business_state_change(stop_step: str) -> None:
    db = make_v4_db()
    try:
        db.upsert_role_instance(
            role_instance_id="agentic-mesh-dev.engineering.1",
            role_id=ROLE_ID,
            display_name="Engineering",
            service_name="agentic-mesh-dev-engineering-1",
            authority="full",
            codex_endpoint="ws://synthetic:4700",
        )
        db.upsert_work_item(
            work_item_id="retained-work",
            title="Retained synthetic work",
            state="planned",
            owner_role=ROLE_ID,
            next_action="remain",
        )
        before = _retained_business_records(db)
        with pytest.raises(InterruptedError, match=f"after {stop_step.replace('_', ' ')}"):
            apply_disposable_role_metadata(
                db.connection,
                project_id="agentic-mesh-dev",
                sources=(source("agentic-mesh-dev"),),
                control_schema=True,
                permit=STAGE1_DISPOSABLE_PERMIT,
                stop_after_step=stop_step,
            )
        rollback_disposable_role_metadata(db.connection, permit=STAGE1_DISPOSABLE_PERMIT)
        assert _retained_business_records(db) == before
        legacy = db.connection.execute(
            "SELECT identity_kind, fleet_instance_id, assigned_project_id FROM role_instances WHERE role_instance_id=?",
            ("agentic-mesh-dev.engineering.1",),
        ).fetchone()
        assert legacy == {
            "identity_kind": "legacy_project_instance",
            "fleet_instance_id": None,
            "assigned_project_id": None,
        }
    finally:
        db.close()


def _business_counts(db: V4Database) -> tuple[int, int, int]:
    return tuple(
        int(db.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
        for table in ("work_items", "message_queue", "project_memory")
    )


def _retained_business_records(db: V4Database) -> dict[str, object]:
    schema = str(db.connection.execute("SELECT current_schema() AS schema").fetchone()["schema"])
    catalog = discover_postgres_catalog(
        db.connection,
        project_id="agentic-mesh-dev",
        schema=schema,
        control_schema=False,
    )
    return {
        table: records
        for table, records in catalog.records.items()
        if table != "role_instances"
    }
