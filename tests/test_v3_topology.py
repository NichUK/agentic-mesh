from pathlib import Path

from agentic_mesh_v3.topology import V3Topology
from agentic_mesh_v3.topology import validate_topology


def test_topology_rejects_collapsed_runtime_and_source_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    topology = V3Topology(
        source_repo=source,
        deployed_runtime=source,
        runtime_state=tmp_path / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=tmp_path / "documents",
    )

    errors = validate_topology(topology)

    assert any("source_repo and deployed_runtime" in error for error in errors)


def test_topology_allows_distinct_paths(tmp_path: Path) -> None:
    topology = V3Topology(
        source_repo=tmp_path / "source",
        deployed_runtime=tmp_path / "runtime",
        runtime_state=tmp_path / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=tmp_path / "documents",
    )

    assert validate_topology(topology) == []


def test_topology_allows_target_repository_to_match_source_repo(tmp_path: Path) -> None:
    source = tmp_path / "source"
    topology = V3Topology(
        source_repo=source,
        deployed_runtime=tmp_path / "runtime",
        runtime_state=tmp_path / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=tmp_path / "documents",
        target_repositories={"agentic-mesh": source},
    )

    assert validate_topology(topology) == []


def test_topology_rejects_target_repository_inside_runtime_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    topology = V3Topology(
        source_repo=tmp_path / "source",
        deployed_runtime=tmp_path / "runtime",
        runtime_state=state,
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=tmp_path / "documents",
        target_repositories={"app": state / "workspaces" / "app"},
    )

    assert any(
        "target_repository.app must not live inside runtime_state" in error
        for error in validate_topology(topology)
    )


def test_topology_rejects_target_repository_that_contains_document_library(tmp_path: Path) -> None:
    app = tmp_path / "app"
    topology = V3Topology(
        source_repo=tmp_path / "source",
        deployed_runtime=tmp_path / "runtime",
        runtime_state=tmp_path / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=app / "documents",
        target_repositories={"app": app},
    )

    assert any(
        "document_library_root must not live inside target_repository.app" in error
        for error in validate_topology(topology)
    )


def test_topology_rejects_overlapping_target_repositories(tmp_path: Path) -> None:
    app = tmp_path / "app"
    topology = V3Topology(
        source_repo=tmp_path / "source",
        deployed_runtime=tmp_path / "runtime",
        runtime_state=tmp_path / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=tmp_path / "documents",
        target_repositories={"app": app, "nested": app / "nested"},
    )

    assert any(
        "target_repository.nested must not live inside target_repository.app" in error
        for error in validate_topology(topology)
    )


def test_topology_rejects_runtime_state_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    topology = V3Topology(
        source_repo=source,
        deployed_runtime=tmp_path / "runtime",
        runtime_state=source / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=tmp_path / "documents",
    )

    assert any("runtime_state must not live inside source_repo" in error for error in validate_topology(topology))


def test_topology_rejects_project_config_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    topology = V3Topology(
        source_repo=source,
        deployed_runtime=tmp_path / "runtime",
        runtime_state=tmp_path / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=source / "examples" / "projects" / "agentic-mesh-dev",
        document_library_root=tmp_path / "documents",
    )

    assert any("project_config_repo must not live inside source_repo" in error for error in validate_topology(topology))


def test_topology_rejects_source_inside_deployed_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    topology = V3Topology(
        source_repo=runtime / "src",
        deployed_runtime=runtime,
        runtime_state=tmp_path / "state",
        organisation_config_repo=tmp_path / "org",
        project_config_repo=tmp_path / "project",
        document_library_root=tmp_path / "documents",
    )

    assert any("source_repo must not live inside deployed_runtime" in error for error in validate_topology(topology))


def test_topology_local_dev_override_allows_nested_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    topology = V3Topology(
        source_repo=source,
        deployed_runtime=source / "runtime",
        runtime_state=source / ".tmp" / "state",
        organisation_config_repo=source / "org",
        project_config_repo=source / "examples" / "projects" / "agentic-mesh-dev",
        document_library_root=source / ".tmp" / "documents",
        target_repositories={"app": source / ".tmp" / "state" / "app"},
        local_dev_override=True,
    )

    assert validate_topology(topology) == []
