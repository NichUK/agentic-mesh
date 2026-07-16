from __future__ import annotations

from pathlib import Path

import pytest

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.shared_fleet import ALL_PUBLIC_TABLES
from agentic_mesh_v4.shared_fleet import COMMON_TABLES
from agentic_mesh_v4.shared_fleet import PUBLIC_ONLY_TABLES
from agentic_mesh_v4.shared_fleet import CatalogSnapshot
from agentic_mesh_v4.shared_fleet import ExistingFleetIdentity
from agentic_mesh_v4.shared_fleet import SourceRoleIdentity
from agentic_mesh_v4.shared_fleet import STAGE1_DISPOSABLE_PERMIT
from agentic_mesh_v4.shared_fleet import apply_disposable_role_metadata
from agentic_mesh_v4.shared_fleet import build_reconciliation_manifest
from agentic_mesh_v4.shared_fleet import discover_postgres_catalog
from agentic_mesh_v4.shared_fleet import reconcile_identities

from shared_fleet_support import ROLE_ID
from shared_fleet_support import source
from v4_postgres import make_v4_db


def test_closed_ledger_and_manifest_are_exhaustive_content_free_and_deterministic() -> None:
    assert len(COMMON_TABLES) == 32
    assert len(PUBLIC_ONLY_TABLES) == 4
    assert len(ALL_PUBLIC_TABLES) == 36
    identities = reconcile_identities(
        fleet_id="agentic-mesh",
        sources=(source("orchid"), source("cedar")),
    )
    catalogs = (
        CatalogSnapshot("agentic-mesh-dev", "public", True, {table: 0 for table in ALL_PUBLIC_TABLES}),
        CatalogSnapshot("quantauma", "quantauma", False, {table: 0 for table in COMMON_TABLES}),
    )

    first = build_reconciliation_manifest(catalogs=catalogs, identities=identities)
    second = build_reconciliation_manifest(catalogs=tuple(reversed(catalogs)), identities=identities)
    assert first.digest == second.digest
    assert not first.blocking
    assert len(first.payload["catalogs"]) == 72
    skips = [item for item in first.payload["catalogs"] if item["action"] == "skipped_absent"]
    assert len(skips) == 4
    rendered = str(first.as_dict()).lower()
    for forbidden in ("message text", "credential value", "document content", "repository content"):
        assert forbidden not in rendered


def test_identity_duplicate_unknown_and_divergent_rules_stop_deterministically() -> None:
    matching = reconcile_identities(
        fleet_id="agentic-mesh",
        sources=(source("orchid"), source("cedar")),
        existing={
            "agentic-mesh.engineering.1": ExistingFleetIdentity(
                "agentic-mesh.engineering.1",
                "role-v1",
            )
        },
    )
    assert not matching.blocking
    assert "duplicate_noop" in {item.action for item in matching.actions}

    unknown = SourceRoleIdentity("orchid", "wrong.engineering.1", ROLE_ID, 1, "role-v1")
    result = reconcile_identities(fleet_id="agentic-mesh", sources=(unknown,))
    assert result.blocking
    assert result.actions[0].action == "skipped_unknown_identity"

    divergent = reconcile_identities(
        fleet_id="agentic-mesh",
        sources=(source("orchid"), source("cedar", fingerprint="role-v2")),
    )
    assert divergent.blocking
    assert "hard_conflict" in {item.action for item in divergent.actions}


def test_disposable_postgres_catalog_dry_run_and_interrupted_apply_are_idempotent() -> None:
    public_db = make_v4_db()
    quantauma_db = make_v4_db()
    try:
        public_schema = _schema(public_db)
        quantauma_schema = _schema(quantauma_db)
        _add_public_only_tables(public_db)
        _register_source(public_db, "agentic-mesh-dev")
        _register_source(quantauma_db, "quantauma")
        public_catalog = discover_postgres_catalog(
            public_db.connection,
            project_id="agentic-mesh-dev",
            schema=public_schema,
            control_schema=True,
        )
        quantauma_catalog = discover_postgres_catalog(
            quantauma_db.connection,
            project_id="quantauma",
            schema=quantauma_schema,
            control_schema=False,
        )
        identities = reconcile_identities(
            fleet_id="agentic-mesh",
            sources=(source("agentic-mesh-dev"), source("quantauma")),
        )
        before = build_reconciliation_manifest(
            catalogs=(public_catalog, quantauma_catalog),
            identities=identities,
        )
        repeat = build_reconciliation_manifest(
            catalogs=(public_catalog, quantauma_catalog),
            identities=identities,
        )
        assert before.digest == repeat.digest and not before.blocking

        with pytest.raises(InterruptedError, match="after assignments"):
            apply_disposable_role_metadata(
                public_db.connection,
                project_id="agentic-mesh-dev",
                sources=(source("agentic-mesh-dev"),),
                control_schema=True,
                permit=STAGE1_DISPOSABLE_PERMIT,
                stop_after_step="assignments",
            )
        rerun = apply_disposable_role_metadata(
            public_db.connection,
            project_id="agentic-mesh-dev",
            sources=(source("agentic-mesh-dev"),),
            control_schema=True,
            permit=STAGE1_DISPOSABLE_PERMIT,
        )
        q_result = apply_disposable_role_metadata(
            quantauma_db.connection,
            project_id="quantauma",
            sources=(source("quantauma"),),
            control_schema=False,
            permit=STAGE1_DISPOSABLE_PERMIT,
        )
        assert rerun.duplicate_noop >= 1
        assert q_result.completed_steps == ("schema", "assignments")
        assert _count(public_db, "work_items") == 0
        assert _count(quantauma_db, "work_items") == 0
    finally:
        public_db.close()
        quantauma_db.close()


def _schema(db: V4Database) -> str:
    return str(db.connection.execute("SELECT current_schema() AS schema").fetchone()["schema"])


def _add_public_only_tables(db: V4Database) -> None:
    for table in PUBLIC_ONLY_TABLES:
        db.connection.execute(f"CREATE TABLE {table}(record_id TEXT PRIMARY KEY)")


def _register_source(db: V4Database, project_id: str) -> None:
    db.upsert_role_instance(
        role_instance_id=f"{project_id}.{ROLE_ID}.1",
        role_id=ROLE_ID,
        display_name="Engineering",
        service_name=f"{project_id}-engineering-1",
        authority="full",
        codex_endpoint="ws://synthetic:4700",
    )


def _count(db: V4Database, table: str) -> int:
    return int(db.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
