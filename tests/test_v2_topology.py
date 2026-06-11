from pathlib import Path

import pytest

from agentic_mesh_v2.topology import ProjectRepo
from agentic_mesh_v2.topology import RuntimeTopology
from agentic_mesh_v2.topology import TopologyError


def test_topology_requires_source_runtime_state_and_project_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "agentic-mesh-source"
    deployed = tmp_path / "agentic-mesh-runtime"
    state = tmp_path / "agentic-mesh-state"
    project = tmp_path / "customer-project"
    docs = project / "docs"
    for path in [source, deployed, state, docs]:
        path.mkdir(parents=True)

    topology = RuntimeTopology(
        source_repo=source,
        deployed_runtime=deployed,
        runtime_state=state,
        project_repos=(
            ProjectRepo(
                repo_id="customer",
                path=project,
                document_library_root=docs,
                target_repositories=(source,),
            ),
        ),
        image_identity="agentic-mesh:v2-test",
    ).validate()

    assert topology.source_repo == source.resolve()
    assert topology.deployed_runtime == deployed.resolve()
    assert topology.project_repos[0].target_repositories == (source.resolve(),)


def test_topology_rejects_project_repo_inside_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = source / "examples" / "project"
    deployed = tmp_path / "runtime"
    state = tmp_path / "state"
    docs = project / "docs"
    for path in [source, deployed, state, docs]:
        path.mkdir(parents=True)

    with pytest.raises(TopologyError, match="must not live inside"):
        RuntimeTopology(
            source_repo=source,
            deployed_runtime=deployed,
            runtime_state=state,
            project_repos=(
                ProjectRepo(
                    repo_id="dogfood",
                    path=project,
                    document_library_root=docs,
                ),
            ),
        ).validate()


def test_local_dev_overlap_requires_explicit_reason(tmp_path: Path) -> None:
    root = tmp_path / "all-in-one"
    root.mkdir()

    with pytest.raises(TopologyError, match="requires an explicit reason"):
        RuntimeTopology(
            source_repo=root,
            deployed_runtime=root,
            runtime_state=root,
            project_repos=(),
            allow_local_dev_overlap=True,
        ).validate()

    topology = RuntimeTopology(
        source_repo=root,
        deployed_runtime=root,
        runtime_state=root,
        project_repos=(),
        allow_local_dev_overlap=True,
        local_dev_reason="unit test",
    ).validate()
    assert topology.warnings
