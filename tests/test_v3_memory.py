from __future__ import annotations

from pathlib import Path

from agentic_mesh_v3.db import V3Database
from agentic_mesh_v3.memory import DatabaseRoleMemory
from agentic_mesh_v3.memory import RoleMemoryRecord
from agentic_mesh_v3.memory import SQLiteRoleMemory
from agentic_mesh_v3.tools import V3ToolService


def test_sqlite_role_memory_records_source_linked_summary(tmp_path: Path) -> None:
    memory = SQLiteRoleMemory(tmp_path / "product-manager-memory.sqlite3")
    try:
        memory.record(
            RoleMemoryRecord(
                role_instance_id="agentic-mesh-dev.product-manager.1",
                summary="Sponsor prefers compact dashboard rows.",
                source_ref="work-123/index.md",
            )
        )

        summary = memory.load_summary("agentic-mesh-dev.product-manager.1")

        assert "Sponsor prefers compact dashboard rows." in summary
        assert "source: work-123/index.md" in summary
    finally:
        memory.close()


def test_sqlite_role_memory_deduplicates_exact_source_linked_record(tmp_path: Path) -> None:
    memory = SQLiteRoleMemory(tmp_path / "product-manager-memory.sqlite3")
    try:
        record = RoleMemoryRecord(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            summary="Sponsor prefers compact dashboard rows.",
            source_ref="work-123/index.md",
        )
        memory.record(record)
        memory.record(record)

        assert memory.load_summary("agentic-mesh-dev.product-manager.1").count("Sponsor prefers") == 1
    finally:
        memory.close()


def test_memory_safe_output_records_role_memory(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        service = V3ToolService(db)

        result = service.call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="memory.propose_update",
            payload={
                "summary": "Sponsor prefers compact dashboard rows.",
                "source_ref": "work-123/index.md",
            },
        )

        role_memory = db.list_role_memory("agentic-mesh-dev.product-manager.1")
        assert role_memory == [
            {
                "memory_id": f"memory-{result.call_id}",
                "role_instance_id": "agentic-mesh-dev.product-manager.1",
                "summary": "Sponsor prefers compact dashboard rows.",
                "source_ref": "work-123/index.md",
                "created_at": role_memory[0]["created_at"],
            }
        ]
    finally:
        db.close()


def test_database_role_memory_reads_safe_output_memory_records(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        V3ToolService(db).call(
            role_instance_id="agentic-mesh-dev.product-manager.1",
            tool_name="memory.propose_update",
            payload={
                "summary": "Sponsor prefers compact dashboard rows.",
                "source_ref": "work-123/index.md",
            },
        )
        memory = DatabaseRoleMemory(db)

        summary = memory.load_summary("agentic-mesh-dev.product-manager.1")

        assert "Sponsor prefers compact dashboard rows." in summary
        assert "source: work-123/index.md" in summary
    finally:
        db.close()


def test_database_role_memory_deduplicates_agent_observations(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        memory = DatabaseRoleMemory(db)

        memory.record_observation("agentic-mesh-dev.product-manager.1", "Processed message msg-123.")
        memory.record_observation("agentic-mesh-dev.product-manager.1", "Processed message msg-123.")

        assert memory.load_summary("agentic-mesh-dev.product-manager.1").count("Processed message msg-123.") == 1
    finally:
        db.close()


def test_database_role_memory_records_observation_source_ref(tmp_path: Path) -> None:
    db = V3Database(tmp_path / "v3.sqlite3")
    try:
        db.migrate()
        memory = DatabaseRoleMemory(db)

        memory.record_observation(
            "agentic-mesh-dev.product-manager.1",
            "Processed message msg-123.",
            source_ref="work-item:work-123",
        )

        assert "source: work-item:work-123" in memory.load_summary("agentic-mesh-dev.product-manager.1")
    finally:
        db.close()
