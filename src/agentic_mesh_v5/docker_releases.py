from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Sequence

from agentic_mesh_v5.immutable_releases import DeploymentObservation
from agentic_mesh_v5.immutable_releases import ImmutableReleaseError
from agentic_mesh_v5.immutable_releases import UpgradeRequest
from agentic_mesh_v5.immutable_releases import VerifiedImage


_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[a-z0-9][a-z0-9./_-]{0,255}$")
_SERVICE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class DockerReleaseError(ImmutableReleaseError):
    pass


class DockerImageBuilder:
    def __init__(
        self,
        *,
        dockerfile: Path,
        image_repository: str,
        test_targets: Sequence[str],
        minimum_database_version: int,
        maximum_database_version: int,
        publish_image: bool = False,
        docker_command: Sequence[str] = ("docker",),
    ) -> None:
        if dockerfile.is_absolute() or ".." in dockerfile.parts:
            raise ValueError("dockerfile must be a source-relative path")
        if _REPOSITORY.fullmatch(image_repository) is None:
            raise ValueError("image_repository is invalid")
        targets = tuple(_relative(item, "test target") for item in test_targets)
        if not targets:
            raise ValueError("at least one test target is required")
        if (
            isinstance(minimum_database_version, bool)
            or not isinstance(minimum_database_version, int)
            or minimum_database_version <= 0
            or isinstance(maximum_database_version, bool)
            or not isinstance(maximum_database_version, int)
            or maximum_database_version < minimum_database_version
        ):
            raise ValueError("database compatibility range is invalid")
        self._dockerfile = dockerfile
        self._repository = image_repository
        self._test_targets = targets
        self._minimum = minimum_database_version
        self._maximum = maximum_database_version
        if not isinstance(publish_image, bool):
            raise ValueError("publish_image must be a boolean")
        self._publish = publish_image
        self._docker = _command(docker_command, "docker_command")

    def build_and_verify(
        self, request: UpgradeRequest, *, release_id: str
    ) -> VerifiedImage:
        source = request.source_root.resolve()
        dockerfile = source / self._dockerfile
        if not dockerfile.is_file():
            raise DockerReleaseError("runtime Dockerfile is missing")
        for target in self._test_targets:
            if not (source / target).exists():
                raise DockerReleaseError(f"test target is missing: {target}")
        head = self._git(source, "rev-parse", "HEAD").lower()
        if head != request.source_revision:
            raise DockerReleaseError("source checkout does not match source_revision")
        if self._git(source, "status", "--porcelain=v1"):
            raise DockerReleaseError("source checkout must be clean before release")
        tested = self._run(
            [sys.executable, "-m", "pytest", "-q", *self._test_targets],
            cwd=source,
            timeout=1800,
        )
        if self._git(source, "status", "--porcelain=v1"):
            raise DockerReleaseError("release tests changed the source checkout")
        source_date_epoch = self._git(
            source, "show", "-s", "--format=%ct", request.source_revision
        )
        if not source_date_epoch.isdigit():
            raise DockerReleaseError("source commit timestamp is invalid")
        tag = f"{self._repository}:candidate-{release_id[:12]}"
        built = self._run(
            [
                *self._docker,
                "build",
                "--file",
                str(dockerfile),
                "--tag",
                tag,
                "--build-arg",
                f"VCS_REF={request.source_revision}",
                "--build-arg",
                f"SOURCE_DATE_EPOCH={source_date_epoch}",
                str(source),
            ],
            cwd=source,
            timeout=1800,
        )
        published_output = ""
        if self._publish:
            published = self._run(
                [*self._docker, "push", tag], cwd=source, timeout=1800
            )
            published_output = published.stdout + published.stderr
        inspected = self._run(
            [*self._docker, "image", "inspect", tag], cwd=source, timeout=60
        )
        image_ref = self._inspect_image(inspected.stdout, request.source_revision)
        smoke = self._run(
            [
                *self._docker,
                "run",
                "--rm",
                image_ref,
                "--json",
                "boundary-check",
            ],
            cwd=source,
            timeout=300,
        )
        try:
            smoke_payload = json.loads(smoke.stdout)
        except json.JSONDecodeError as exc:
            raise DockerReleaseError("image smoke output is invalid") from exc
        if smoke_payload.get("status") != "clean":
            raise DockerReleaseError("image smoke probe rejected the candidate")
        return VerifiedImage(
            image_ref=image_ref,
            minimum_database_version=self._minimum,
            maximum_database_version=self._maximum,
            build_evidence_ref=_evidence_ref(
                built.stdout + built.stderr + published_output + inspected.stdout
            ),
            test_evidence_ref=_evidence_ref(tested.stdout + tested.stderr + smoke.stdout),
        )

    def _inspect_image(self, value: str, source_revision: str) -> str:
        try:
            payload = json.loads(value)
            image = payload[0]
            labels = image["Config"]["Labels"]
            digests = image["RepoDigests"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DockerReleaseError("Docker image metadata is invalid") from exc
        if labels.get("org.opencontainers.image.revision") != source_revision:
            raise DockerReleaseError("image revision label does not match the source")
        matches = sorted(
            item
            for item in digests or []
            if isinstance(item, str) and item.startswith(f"{self._repository}@sha256:")
        )
        if len(matches) != 1 or _IMAGE.fullmatch(matches[0]) is None:
            raise DockerReleaseError(
                "built image has no unique content-addressed repository digest"
            )
        return matches[0]

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if completed.returncode != 0:
            raise DockerReleaseError(
                f"Git command failed: {' '.join(arguments)}: "
                f"{_diagnostic(completed.stderr)}"
            )
        return completed.stdout.strip()

    @staticmethod
    def _run(
        command: Sequence[str], *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise DockerReleaseError(
                f"release command failed: {_diagnostic(completed.stderr)}"
            )
        return completed


class DockerComposeDeployment:
    def __init__(
        self,
        *,
        compose_file: Path,
        image_environment_file: Path,
        compose_project: str,
        service: str,
        health_timeout_seconds: int = 120,
        poll_seconds: float = 2.0,
        docker_command: Sequence[str] = ("docker",),
    ) -> None:
        self._compose_file = _existing_file(compose_file, "compose_file")
        self._image_environment_file = _existing_file(
            image_environment_file, "image_environment_file"
        )
        if _SERVICE.fullmatch(compose_project) is None:
            raise ValueError("compose_project is invalid")
        if _SERVICE.fullmatch(service) is None:
            raise ValueError("service is invalid")
        if health_timeout_seconds < 1 or poll_seconds <= 0:
            raise ValueError("health timing is invalid")
        self._project = compose_project
        self._service = service
        self._timeout = health_timeout_seconds
        self._poll = poll_seconds
        self._docker = _command(docker_command, "docker_command")

    def validate(self, *, forbidden_source_root: Path) -> None:
        source = forbidden_source_root.resolve()
        payload = self._compose_json("config", "--format", "json")
        try:
            service = payload["services"][self._service]
        except (KeyError, TypeError) as exc:
            raise DockerReleaseError("Compose service is missing") from exc
        if "build" in service:
            raise DockerReleaseError("runtime Compose service cannot build live source")
        image = service.get("image")
        if not isinstance(image, str) or _IMAGE.fullmatch(image) is None:
            raise DockerReleaseError("runtime Compose image must be digest-pinned")
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            value = volume.get("source")
            if not isinstance(value, str):
                raise DockerReleaseError("Compose bind source is invalid")
            if _overlaps(Path(value), source):
                raise DockerReleaseError("runtime Compose binds the source checkout")

    def observe(self, *, project_id: str) -> DeploymentObservation:
        del project_id
        container_id = self._compose("ps", "--quiet", self._service).stdout.strip()
        if not container_id:
            return DeploymentObservation(None, False, _evidence_ref("no-container"))
        inspected = self._run(
            [*self._docker, "inspect", container_id], timeout=60
        )
        try:
            payload = json.loads(inspected.stdout)[0]
            image_ref = payload["Config"]["Image"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DockerReleaseError("deployed container metadata is invalid") from exc
        if not isinstance(image_ref, str) or _IMAGE.fullmatch(image_ref) is None:
            raise DockerReleaseError("deployed container image is not digest-pinned")
        try:
            health = payload["State"]["Health"]["Status"]
        except (KeyError, TypeError) as exc:
            raise DockerReleaseError(
                "deployed container has no health status; add a HEALTHCHECK"
            ) from exc
        if not isinstance(health, str):
            raise DockerReleaseError("deployed container health status is invalid")
        return DeploymentObservation(
            image_ref=image_ref,
            healthy=health == "healthy",
            evidence_ref=_evidence_ref(inspected.stdout),
        )

    def deploy(self, *, project_id: str, image_ref: str) -> None:
        del project_id
        if _IMAGE.fullmatch(image_ref) is None:
            raise DockerReleaseError("deployment image must be digest-pinned")
        self._write_image_environment(image_ref)
        self._compose("up", "-d", "--no-deps", "--force-recreate", self._service)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            observed = self.observe(project_id=self._project)
            if observed.image_ref == image_ref and observed.healthy:
                return
            time.sleep(self._poll)
        raise DockerReleaseError("deployed image did not become healthy before timeout")

    def _write_image_environment(self, image_ref: str) -> None:
        target = self._image_environment_file
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(f"AGENTIC_MESH_V5_IMAGE={image_ref}\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _compose_json(self, *arguments: str) -> dict[str, object]:
        completed = self._compose(*arguments)
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DockerReleaseError("Compose configuration output is invalid") from exc
        if not isinstance(value, dict):
            raise DockerReleaseError("Compose configuration must be an object")
        return value

    def _compose(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                *self._docker,
                "compose",
                "--project-name",
                self._project,
                "--env-file",
                str(self._image_environment_file),
                "--file",
                str(self._compose_file),
                *arguments,
            ],
            timeout=max(self._timeout, 60),
        )

    @staticmethod
    def _run(
        command: Sequence[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise DockerReleaseError(
                f"Docker Compose command failed: {_diagnostic(completed.stderr)}"
            )
        return completed


def _command(value: Sequence[str], field: str) -> tuple[str, ...]:
    command = tuple(value)
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError(f"{field} is invalid")
    return command


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must remain inside the source checkout")
    return path.as_posix()


def _existing_file(value: Path, field: str) -> Path:
    if not value.is_absolute() or not value.is_file():
        raise ValueError(f"{field} must be an existing absolute file")
    return value.resolve()


def _overlaps(left: Path, right: Path) -> bool:
    left_value = Path(os.path.normcase(str(left.resolve())))
    right_value = Path(os.path.normcase(str(right.resolve())))
    try:
        left_value.relative_to(right_value)
        return True
    except ValueError:
        try:
            right_value.relative_to(left_value)
            return True
        except ValueError:
            return False


def _evidence_ref(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _diagnostic(value: str) -> str:
    detail = " ".join(value.split())[:500]
    lowered = detail.casefold()
    if any(marker in lowered for marker in ("password=", "client_secret", "bearer ")):
        return "restricted diagnostic"
    return detail or "no diagnostic"
