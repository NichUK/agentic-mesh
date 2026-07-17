#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


BASE_IMAGE = (
    "python:3.12.11-slim-bookworm@sha256:"
    "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
)


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def _wait_for_status(container_id: str, expected: str) -> str:
    status = "unknown"
    for _ in range(30):
        status = _run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container_id]
        ).stdout.strip()
        if status == expected:
            return status
        time.sleep(0.5)
    raise RuntimeError(f"fixture status remained {status}; expected {expected}")


def _build(image: str, state: str) -> None:
    if state == "broken":
        command = "raise SystemExit(1)"
    else:
        command = "import time; print('repaired', flush=True); time.sleep(300)"
    dockerfile = (
        f"FROM {BASE_IMAGE}\n"
        f'LABEL io.agentic-mesh.recovery-fixture="{state}"\n'
        f'CMD ["python", "-c", "{command}"]\n'
    )
    _run(["docker", "build", "--quiet", "--tag", image, "-"], input_text=dockerfile)


def main() -> int:
    suffix = f"{os.getpid()}-{int(time.time())}"
    project = f"mesh-recovery-{suffix}"
    image = f"agentic-mesh/recovery-drill:{suffix}"
    evidence: dict[str, object] = {
        "status": "failed",
        "project": project,
        "initial_container_status": None,
        "repaired_container_status": None,
        "restarted_container_status": None,
        "repaired_image_label": None,
        "errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="agentic-mesh-recovery-") as directory:
        compose_path = Path(directory) / "compose.yaml"
        compose_path.write_text(
            "services:\n"
            "  mesh:\n"
            f"    image: {image}\n",
            encoding="utf-8",
        )
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(compose_path),
        ]
        try:
            _run(["docker", "info"])
            _build(image, "broken")
            _run([*compose, "up", "--detach"])
            container_id = _run(
                [*compose, "ps", "--all", "--quiet", "mesh"]
            ).stdout.strip()
            if not container_id:
                raise RuntimeError("broken fixture container was not created")
            evidence["initial_container_status"] = _wait_for_status(
                container_id, "exited"
            )

            _build(image, "repaired")
            _run([*compose, "up", "--detach", "--force-recreate"])
            container_id = _run(
                [*compose, "ps", "--all", "--quiet", "mesh"]
            ).stdout.strip()
            evidence["repaired_container_status"] = _wait_for_status(
                container_id, "running"
            )
            _run(["docker", "restart", container_id])
            evidence["restarted_container_status"] = _wait_for_status(
                container_id, "running"
            )
            evidence["repaired_image_label"] = _run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "io.agentic-mesh.recovery-fixture" }}',
                    image,
                ]
            ).stdout.strip()
            if evidence["repaired_image_label"] != "repaired":
                raise RuntimeError("fixture image label was not repaired")
            evidence["status"] = "passed"
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            errors = evidence["errors"]
            assert isinstance(errors, list)
            errors.append(str(exc))
        finally:
            _run([*compose, "down", "--remove-orphans"], check=False)
            _run(["docker", "image", "rm", "--force", image], check=False)

    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
