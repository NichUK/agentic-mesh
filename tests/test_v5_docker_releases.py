from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from agentic_mesh_v5 import cli
from agentic_mesh_v5.cli import build_parser
from agentic_mesh_v5.docker_releases import DockerComposeDeployment
from agentic_mesh_v5.docker_releases import DockerImageBuilder
from agentic_mesh_v5.docker_releases import DockerReleaseError
from agentic_mesh_v5.immutable_releases import DeploymentObservation
from agentic_mesh_v5.immutable_releases import UpgradeRequest


ROOT = Path(__file__).parents[1]
REVISION = "1" * 40
PREVIOUS = "ghcr.io/example/mesh@sha256:" + "a" * 64
CANDIDATE = "ghcr.io/example/mesh@sha256:" + "b" * 64


def _completed(command: list[str], stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(command, 0, stdout, stderr)


def test_runtime_image_and_project_compose_exclude_live_source_mounts() -> None:
    dockerfile = (ROOT / "docker" / "v5" / "runtime.Dockerfile").read_text(
        encoding="utf-8"
    )
    ignore = (
        ROOT / "docker" / "v5" / "runtime.Dockerfile.dockerignore"
    ).read_text(encoding="utf-8")
    compose = (
        ROOT
        / "examples"
        / "projects"
        / "agentic-mesh-v5"
        / "deploy"
        / "compose"
        / "compose.yaml"
    ).read_text(encoding="utf-8")

    assert "python:3.12.11-slim-bookworm@sha256:" in dockerfile
    assert "COPY src/agentic_mesh_v5 ./src/agentic_mesh_v5" in dockerfile
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "USER mesh" in dockerfile
    assert "boundary-check" in dockerfile
    assert not (ROOT / ".dockerignore").exists()
    assert ignore.splitlines()[0] == "*"
    assert "!src/agentic_mesh_v5/**" in ignore
    assert "AGENTIC_MESH_V5_IMAGE" in compose
    assert "agentic-mesh-config" not in compose
    assert "/src" not in compose
    assert "build:" not in compose


def test_docker_builder_uses_exact_commit_tests_digest_and_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    (source / "docker" / "v5").mkdir(parents=True)
    (source / "docker" / "v5" / "runtime.Dockerfile").write_text(
        "FROM scratch\n", encoding="utf-8"
    )
    (source / "tests").mkdir()
    (source / "tests" / "test_release.py").write_text("", encoding="utf-8")
    request = UpgradeRequest(
        project_id="alpha",
        operation_id="upgrade-1",
        source_root=source,
        source_revision=REVISION,
        actor_id="release-manager",
    )
    builder = DockerImageBuilder(
        dockerfile=Path("docker/v5/runtime.Dockerfile"),
        image_repository="ghcr.io/example/mesh",
        test_targets=("tests/test_release.py",),
        minimum_database_version=25,
        maximum_database_version=26,
        publish_image=True,
    )
    git_calls: list[tuple[str, ...]] = []
    command_calls: list[list[str]] = []

    def fake_git(root: Path, *arguments: str) -> str:
        assert root == source
        git_calls.append(arguments)
        if arguments[:2] == ("rev-parse", "HEAD"):
            return REVISION
        if arguments[0] == "status":
            return ""
        if arguments[0] == "show":
            return "1751328000"
        raise AssertionError(arguments)

    def fake_run(command, *, cwd: Path, timeout: int):
        del timeout
        assert cwd == source
        values = list(command)
        command_calls.append(values)
        if values[1:3] == ["image", "inspect"]:
            return _completed(
                values,
                json.dumps(
                    [
                        {
                            "RepoDigests": [CANDIDATE],
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": REVISION
                                }
                            },
                        }
                    ]
                ),
            )
        if values[1:3] == ["run", "--rm"]:
            return _completed(values, '{"status":"clean"}')
        return _completed(values, "passed")

    monkeypatch.setattr(builder, "_git", fake_git)
    monkeypatch.setattr(builder, "_run", fake_run)

    result = builder.build_and_verify(request, release_id="c" * 64)

    assert result.image_ref == CANDIDATE
    assert (result.minimum_database_version, result.maximum_database_version) == (
        25,
        26,
    )
    assert result.build_evidence_ref.startswith("sha256:")
    assert result.test_evidence_ref.startswith("sha256:")
    assert git_calls.count(("status", "--porcelain=v1")) == 2
    test = next(item for item in command_calls if "pytest" in item)
    build = next(item for item in command_calls if "build" in item)
    push = next(item for item in command_calls if "push" in item)
    smoke = next(item for item in command_calls if "boundary-check" in item)
    assert test[-1] == "tests/test_release.py"
    assert f"VCS_REF={REVISION}" in build
    assert build[-1] == str(source)
    assert push[-1].startswith("ghcr.io/example/mesh:candidate-")
    assert command_calls.index(push) < next(
        index
        for index, item in enumerate(command_calls)
        if item[1:3] == ["image", "inspect"]
    )
    assert CANDIDATE in smoke


def test_release_subprocesses_decode_output_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object):
        options.append(kwargs)
        return _completed(command)

    monkeypatch.setattr(subprocess, "run", fake_run)

    DockerImageBuilder._git(tmp_path, "status", "--porcelain=v1")
    DockerImageBuilder._run(["docker", "version"], cwd=tmp_path, timeout=1)
    DockerComposeDeployment._run(["docker", "version"], timeout=1)

    assert len(options) == 3
    assert all(item["encoding"] == "utf-8" for item in options)
    assert all(item["errors"] == "replace" for item in options)


def test_compose_adapter_rejects_source_bind_and_atomically_selects_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "compose.yaml"
    image_env = tmp_path / "image.env"
    source = tmp_path / "source"
    external = tmp_path / "configuration"
    compose.write_text("services: {}\n", encoding="utf-8")
    image_env.write_text(
        f"AGENTIC_MESH_V5_IMAGE={PREVIOUS}\n", encoding="utf-8"
    )
    source.mkdir()
    external.mkdir()
    deployment = DockerComposeDeployment(
        compose_file=compose,
        image_environment_file=image_env,
        compose_project="mesh-v5",
        service="control",
        health_timeout_seconds=2,
        poll_seconds=0.01,
    )

    def config_with(source_path: Path):
        return {
            "services": {
                "control": {
                    "image": PREVIOUS,
                    "volumes": [{"type": "bind", "source": str(source_path)}],
                }
            }
        }

    monkeypatch.setattr(deployment, "_compose_json", lambda *args: config_with(source))
    with pytest.raises(DockerReleaseError, match="source checkout"):
        deployment.validate(forbidden_source_root=source)
    monkeypatch.setattr(
        deployment, "_compose_json", lambda *args: config_with(external)
    )
    deployment.validate(forbidden_source_root=source)

    compose_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        deployment,
        "_compose",
        lambda *args: compose_calls.append(args) or _completed(list(args)),
    )

    def observe(*, project_id: str):
        assert project_id == "mesh-v5"
        selected = image_env.read_text(encoding="utf-8").strip().split("=", 1)[1]
        return DeploymentObservation(selected, True, "observe:healthy")

    monkeypatch.setattr(deployment, "observe", observe)
    deployment.deploy(project_id="alpha", image_ref=CANDIDATE)

    assert image_env.read_text(encoding="utf-8") == (
        f"AGENTIC_MESH_V5_IMAGE={CANDIDATE}\n"
    )
    assert compose_calls == [
        ("up", "-d", "--no-deps", "--force-recreate", "control")
    ]
    assert not list(tmp_path.glob(".*.tmp"))


def test_compose_observation_requires_digest_and_container_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "compose.yaml"
    image_env = tmp_path / "image.env"
    compose.write_text("services: {}\n", encoding="utf-8")
    image_env.write_text(
        f"AGENTIC_MESH_V5_IMAGE={PREVIOUS}\n", encoding="utf-8"
    )
    deployment = DockerComposeDeployment(
        compose_file=compose,
        image_environment_file=image_env,
        compose_project="mesh-v5",
        service="control",
    )
    monkeypatch.setattr(
        deployment,
        "_compose",
        lambda *args: _completed(list(args), "container-1\n"),
    )
    monkeypatch.setattr(
        deployment,
        "_run",
        lambda command, timeout: _completed(
            list(command),
            json.dumps(
                [
                    {
                        "Config": {"Image": CANDIDATE},
                        "State": {"Health": {"Status": "healthy"}},
                    }
                ]
            ),
        ),
    )

    observed = deployment.observe(project_id="alpha")

    assert observed.image_ref == CANDIDATE
    assert observed.healthy is True
    assert observed.evidence_ref.startswith("sha256:")


def test_compose_observation_explains_missing_healthcheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "compose.yaml"
    image_env = tmp_path / "image.env"
    compose.write_text("services: {}\n", encoding="utf-8")
    image_env.write_text(
        f"AGENTIC_MESH_V5_IMAGE={PREVIOUS}\n", encoding="utf-8"
    )
    deployment = DockerComposeDeployment(
        compose_file=compose,
        image_environment_file=image_env,
        compose_project="mesh-v5",
        service="control",
    )
    monkeypatch.setattr(
        deployment,
        "_compose",
        lambda *args: _completed(list(args), "container-1\n"),
    )
    monkeypatch.setattr(
        deployment,
        "_run",
        lambda command, timeout: _completed(
            list(command),
            json.dumps([{"Config": {"Image": CANDIDATE}, "State": {}}]),
        ),
    )

    with pytest.raises(DockerReleaseError, match="add a HEALTHCHECK"):
        deployment.observe(project_id="alpha")


def test_runtime_upgrade_cli_requires_explicit_project_release_inputs() -> None:
    args = build_parser().parse_args(
        [
            "runtime-upgrade",
            "--project",
            "agentic-mesh-v5",
            "--operation",
            "AMV5-047",
            "--source-root",
            str(ROOT),
            "--source-revision",
            REVISION,
            "--image-repository",
            "ghcr.io/example/mesh",
            "--test-target",
            "tests/test_v5_runtime_boundary.py",
            "--compose-file",
            str(ROOT / "examples" / "compose.yaml"),
            "--image-env-file",
            str(ROOT / "state" / "image.env"),
            "--compose-project",
            "mesh-v5",
            "--actor",
            "release-manager",
        ]
    )

    assert args.project == "agentic-mesh-v5"
    assert args.operation == "AMV5-047"
    assert args.dockerfile == Path("docker/v5/runtime.Dockerfile")
    assert args.service == "control"
    assert args.publish_image is False


def test_runtime_upgrade_cli_returns_failure_for_non_deployed_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Status:
        available_version = 28

    class Runner:
        def __init__(self, database_url: str) -> None:
            assert database_url == "postgresql://test"

        def status(self) -> Status:
            return Status()

    class Result:
        status = "rolled_back"

        def to_dict(self) -> dict[str, object]:
            return {"project_id": "agentic-mesh-v5", "status": self.status}

    class Coordinator:
        def __init__(self, database_url: str, **ports: object) -> None:
            assert database_url == "postgresql://test"
            assert set(ports) == {"builder", "deployment"}

        def upgrade(self, request: UpgradeRequest) -> Result:
            assert request.operation_id == "AMV5-047-qualification"
            return Result()

    monkeypatch.setattr(
        cli, "database_url_from_environment", lambda: "postgresql://test"
    )
    monkeypatch.setattr(cli, "MigrationRunner", Runner)
    monkeypatch.setattr(cli, "DockerImageBuilder", lambda **kwargs: kwargs)
    monkeypatch.setattr(cli, "DockerComposeDeployment", lambda **kwargs: kwargs)
    monkeypatch.setattr(cli, "ImmutableReleaseCoordinator", Coordinator)

    exit_code = cli.main(
        [
            "--json",
            "runtime-upgrade",
            "--project",
            "agentic-mesh-v5",
            "--operation",
            "AMV5-047-qualification",
            "--source-root",
            str(ROOT),
            "--source-revision",
            REVISION,
            "--image-repository",
            "ghcr.io/example/mesh",
            "--test-target",
            "tests/test_v5_runtime_boundary.py",
            "--compose-file",
            str(tmp_path / "compose.yaml"),
            "--image-env-file",
            str(tmp_path / "image.env"),
            "--compose-project",
            "mesh-v5",
            "--actor",
            "release-manager",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "rolled_back"
