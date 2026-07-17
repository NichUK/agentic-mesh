from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.cli import _shared_fleet_manifest_from_capture
from agentic_mesh_v4.cli import build_parser
from agentic_mesh_v4.cli import main
from agentic_mesh_v4.shared_fleet import ALL_PUBLIC_TABLES
from agentic_mesh_v4.shared_fleet import COMMON_TABLES
from agentic_mesh_v4.shared_fleet import PUBLIC_ONLY_TABLES
from agentic_mesh_v4.shared_fleet import CatalogSnapshot
from agentic_mesh_v4.shared_fleet import CatalogRecord
from agentic_mesh_v4.shared_fleet import ExistingFleetIdentity
from agentic_mesh_v4.shared_fleet import FILESYSTEM_STATE_CLASSES
from agentic_mesh_v4.shared_fleet import FilesystemStateSnapshot
from agentic_mesh_v4.shared_fleet import RECORD_ACTIONS
from agentic_mesh_v4.shared_fleet import PUBLIC_ONLY_LEDGER
from agentic_mesh_v4.shared_fleet import STATE_LEDGER
from agentic_mesh_v4.shared_fleet import SourceRoleIdentity
from agentic_mesh_v4.shared_fleet import SharedFleetActivationClosed
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
        CatalogSnapshot("example-project", "example_project", False, {table: 0 for table in COMMON_TABLES}),
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


def test_record_digests_detect_equal_count_divergence_and_close_all_actions() -> None:
    identities = reconcile_identities(fleet_id="agentic-mesh", sources=(source("orchid"),))
    records = tuple(
        CatalogRecord(
            source_key={"message_id": f"m-{index}"},
            allowed_metadata={"state": "completed", "target_role": ROLE_ID},
            action=action,
            reason=f"synthetic_{action}",
        )
        for index, action in enumerate(sorted(RECORD_ACTIONS))
    )
    counts = {table: 0 for table in ALL_PUBLIC_TABLES}
    counts["message_queue"] = len(records)
    catalog = CatalogSnapshot(
        "orchid",
        "public",
        True,
        counts,
        records={"message_queue": records},
    )
    manifest = build_reconciliation_manifest(catalogs=(catalog,), identities=identities)
    reordered = build_reconciliation_manifest(
        catalogs=(
            CatalogSnapshot(
                "orchid",
                "public",
                True,
                counts,
                records={"message_queue": tuple(reversed(records))},
            ),
        ),
        identities=identities,
    )
    assert reordered.digest == manifest.digest
    actions = {
        record["action"]
        for table in manifest.payload["catalogs"]
        for record in table["record_actions"]
    }
    assert actions == RECORD_ACTIONS
    assert manifest.blocking
    assert manifest.payload["terminal"] is True
    assert manifest.payload["terminal_status"] == "blocked"

    matching = CatalogRecord(
        source_key={"message_id": "same-count"},
        allowed_metadata={"state": "completed"},
        target_key={"message_id": "same-count"},
        target_allowed_metadata={"state": "completed"},
    )
    divergent = CatalogRecord(
        source_key={"message_id": "same-count"},
        allowed_metadata={"state": "completed"},
        target_key={"message_id": "same-count"},
        target_allowed_metadata={"state": "failed"},
    )
    equal_counts = {table: 0 for table in ALL_PUBLIC_TABLES}
    equal_counts["message_queue"] = 1
    matching_manifest = build_reconciliation_manifest(
        catalogs=(CatalogSnapshot("orchid", "public", True, equal_counts, {"message_queue": (matching,)}),),
        identities=identities,
    )
    divergent_manifest = build_reconciliation_manifest(
        catalogs=(CatalogSnapshot("orchid", "public", True, equal_counts, {"message_queue": (divergent,)}),),
        identities=identities,
    )
    assert not matching_manifest.blocking
    assert matching_manifest.payload["terminal_status"] == "ready_for_authorization_review"
    assert matching_manifest.payload["action_counts"]["duplicate_noop"] >= 1
    assert divergent_manifest.blocking
    assert matching_manifest.digest != divergent_manifest.digest
    assert any(item["reason"] == "allowed_metadata_content_conflict" for item in divergent_manifest.payload["conflicts"])


def test_filesystem_classes_and_administrative_capture_entrypoint_are_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    filesystem = tuple(
        FilesystemStateSnapshot(
            project_id="agentic-mesh-dev",
            state_class=state_class,
            source_key={"class": state_class},
            allowed_metadata={"binding": "agentic-mesh-dev", "generation": 1},
        )
        for state_class in FILESYSTEM_STATE_CLASSES
    )
    public_records = {
        table: (
            CatalogRecord(
                source_key={"record_id": f"public:{table}:1"},
                allowed_metadata={"state": "retained", "version": 1},
            ),
        )
        for table in ALL_PUBLIC_TABLES
    }
    example_project_records = {
        table: (
            CatalogRecord(
                source_key={"record_id": f"example-project:{table}:1"},
                allowed_metadata={"state": "retained", "version": 1},
            ),
        )
        for table in COMMON_TABLES
    }
    catalogs = (
        CatalogSnapshot(
            "agentic-mesh-dev",
            "public",
            True,
            {table: 1 for table in ALL_PUBLIC_TABLES},
            public_records,
        ),
        CatalogSnapshot(
            "example-project",
            "example_project",
            False,
            {table: 1 for table in COMMON_TABLES},
            example_project_records,
        ),
    )
    manifest = build_reconciliation_manifest(
        catalogs=catalogs,
        identities=reconcile_identities(
            fleet_id="agentic-mesh",
            sources=(source("agentic-mesh-dev"), source("example-project")),
        ),
        filesystem_state=filesystem,
    )
    assert {item["state_class"] for item in manifest.payload["filesystem_state"]} == set(FILESYSTEM_STATE_CLASSES)
    assert all(
        item["qualified_source_key_digest"] and item["allowed_metadata_digest"]
        for item in manifest.payload["filesystem_state"]
    )
    assert len(manifest.payload["catalogs"]) == 72
    assert sum(len(item["record_actions"]) for item in manifest.payload["catalogs"]) == 68
    assert all(item["row_count"] == 1 for item in manifest.payload["catalogs"] if item["action"] != "skipped_absent")
    assert all(
        record["qualified_source_key_digest"] and record["allowed_metadata_digest"]
        for item in manifest.payload["catalogs"]
        for record in item["record_actions"]
    )
    assert len(manifest.payload["retained_state"]) == len(STATE_LEDGER) + len(PUBLIC_ONLY_LEDGER)

    capture = {
        "identities": [
            {
                "project_id": "agentic-mesh-dev",
                "role_instance_id": "agentic-mesh-dev.engineering.1",
                "role_id": "engineering",
                "ordinal": 1,
                "canonical_role_fingerprint": "role-v1",
            },
            {
                "project_id": "example-project",
                "role_instance_id": "example-project.engineering.1",
                "role_id": "engineering",
                "ordinal": 1,
                "canonical_role_fingerprint": "role-v1",
            },
        ],
        "catalogs": [
            {
                "project_id": "agentic-mesh-dev",
                "schema": "public",
                "control_schema": True,
                "tables": {
                    table: [
                        {
                            "source_key": {"record_id": f"public:{table}:1"},
                            "allowed_metadata": {"state": "retained", "version": 1},
                        }
                    ]
                    for table in ALL_PUBLIC_TABLES
                },
            },
            {
                "project_id": "example-project",
                "schema": "example_project",
                "control_schema": False,
                "tables": {
                    table: [
                        {
                            "source_key": {"record_id": f"example-project:{table}:1"},
                            "allowed_metadata": {"state": "retained", "version": 1},
                        }
                    ]
                    for table in COMMON_TABLES
                },
            },
        ],
        "filesystem_state": [
            {
                "project_id": "agentic-mesh-dev",
                "state_class": state_class,
                "source_key": {"class": state_class},
                "allowed_metadata": {"binding": "agentic-mesh-dev", "generation": 1},
            }
            for state_class in FILESYSTEM_STATE_CLASSES
        ],
    }
    captured = _shared_fleet_manifest_from_capture(capture=capture, fleet_id="agentic-mesh")
    assert captured.digest == _shared_fleet_manifest_from_capture(capture=capture, fleet_id="agentic-mesh").digest
    parsed = build_parser().parse_args(
        ["--project-config", str(tmp_path / "project-v4.yaml"), "shared-fleet-reconcile", "--capture", str(tmp_path / "capture.json")]
    )
    assert parsed.command == "shared-fleet-reconcile"
    assert parsed.apply is False
    capture_path = tmp_path / "capture.json"
    output_path = tmp_path / "manifest.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    project_config = Path("examples/projects/agentic-mesh-dev/agentic-mesh/project-v4.yaml")
    main(
        [
            "--project-config",
            str(project_config),
            "shared-fleet-reconcile",
            "--capture",
            str(capture_path),
            "--output",
            str(output_path),
        ]
    )
    capsys.readouterr()
    assert json.loads(output_path.read_text(encoding="utf-8"))["terminal_status"] == "ready_for_authorization_review"
    with pytest.raises(SharedFleetActivationClosed, match="apply is closed"):
        main(
            [
                "--project-config",
                str(project_config),
                "shared-fleet-reconcile",
                "--capture",
                str(capture_path),
                "--apply",
            ]
        )


def test_filesystem_conflicts_block_terminal_manifest_across_all_closed_classes() -> None:
    filesystem = tuple(
        FilesystemStateSnapshot(
            project_id="agentic-mesh-dev",
            state_class=state_class,
            source_key={"class": state_class},
            allowed_metadata={"binding": "agentic-mesh-dev", "generation": 1},
            target_key={"class": state_class},
            target_allowed_metadata={"binding": "foreign", "generation": 1},
        )
        for state_class in FILESYSTEM_STATE_CLASSES
    )
    manifest = build_reconciliation_manifest(
        catalogs=(
            CatalogSnapshot(
                "agentic-mesh-dev",
                "public",
                True,
                {table: 0 for table in ALL_PUBLIC_TABLES},
            ),
        ),
        identities=reconcile_identities(
            fleet_id="agentic-mesh",
            sources=(source("agentic-mesh-dev"),),
        ),
        filesystem_state=filesystem,
    )
    assert {item["action"] for item in manifest.payload["filesystem_state"]} == {"hard_conflict"}
    assert {item["table"] for item in manifest.payload["conflicts"]} == set(FILESYSTEM_STATE_CLASSES)
    assert all(
        item["reason"] == "allowed_metadata_content_conflict"
        and item["schema"] == "filesystem_config"
        for item in manifest.payload["conflicts"]
    )
    assert manifest.blocking is True
    assert manifest.payload["blocking"] is True
    assert manifest.payload["terminal"] is True
    assert manifest.payload["terminal_status"] == "blocked"


def test_disposable_postgres_catalog_dry_run_and_interrupted_apply_are_idempotent() -> None:
    public_db = make_v4_db()
    example_project_db = make_v4_db()
    try:
        public_schema = _schema(public_db)
        example_project_schema = _schema(example_project_db)
        _add_public_only_tables(public_db)
        _register_source(public_db, "agentic-mesh-dev")
        _register_source(example_project_db, "example-project")
        public_catalog = discover_postgres_catalog(
            public_db.connection,
            project_id="agentic-mesh-dev",
            schema=public_schema,
            control_schema=True,
        )
        example_project_catalog = discover_postgres_catalog(
            example_project_db.connection,
            project_id="example-project",
            schema=example_project_schema,
            control_schema=False,
        )
        identities = reconcile_identities(
            fleet_id="agentic-mesh",
            sources=(source("agentic-mesh-dev"), source("example-project")),
        )
        before = build_reconciliation_manifest(
            catalogs=(public_catalog, example_project_catalog),
            identities=identities,
        )
        repeat = build_reconciliation_manifest(
            catalogs=(public_catalog, example_project_catalog),
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
            example_project_db.connection,
            project_id="example-project",
            sources=(source("example-project"),),
            control_schema=False,
            permit=STAGE1_DISPOSABLE_PERMIT,
        )
        assert rerun.duplicate_noop >= 1
        assert q_result.completed_steps == ("schema", "assignments")
        assert _count(public_db, "work_items") == 0
        assert _count(example_project_db, "work_items") == 0
    finally:
        public_db.close()
        example_project_db.close()


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
