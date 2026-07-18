from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).parents[1]
CONTROL_PATH = ROOT / "docker" / "v5" / "recovery-control.py"


def _load_control() -> ModuleType:
    spec = importlib.util.spec_from_file_location("recovery_control", CONTROL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


control = _load_control()


class RecordingPullRequests:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **values: object) -> str:
        self.calls.append(values)
        return "https://github.com/example/mesh/pull/17"


def _run(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        list(argv), cwd=cwd, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _fixture(
    tmp_path: Path,
    *,
    repair: str = "repair",
    verify: str = "verify",
    rollback: str = "rollback",
):
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "workspaces"
    evidence = tmp_path / "evidence"
    deployment = tmp_path / "deployment"
    helper = tmp_path / "fixture.py"
    source.mkdir()
    deployment.mkdir()
    _run("git", "init", "--bare", str(remote), cwd=tmp_path)
    _run("git", "init", cwd=source)
    _run("git", "config", "user.name", "Fixture", cwd=source)
    _run("git", "config", "user.email", "fixture@example.invalid", cwd=source)
    (source / "src").mkdir()
    (source / "src" / "app.txt").write_text("broken\n", encoding="utf-8")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    _run("git", "add", ".", cwd=source)
    _run("git", "commit", "-m", "Initial broken fixture", cwd=source)
    base = _run("git", "rev-parse", "HEAD", cwd=source)
    _run("git", "remote", "add", "origin", str(remote), cwd=source)
    _run("git", "push", "origin", "HEAD:refs/heads/develop", cwd=source)
    (deployment / "revision").write_text(base, encoding="utf-8")
    helper.write_text(
        """from pathlib import Path
import os
import shutil
import sys

stage = sys.argv[1]
deployment = Path(sys.argv[2])
if stage.startswith('repair'):
    Path('src/app.txt').write_text('fixed\\n', encoding='utf-8')
    if stage == 'repair-unrelated':
        Path('README.md').write_text('unrelated\\n', encoding='utf-8')
    usage = 1001 if stage == 'repair-over-limit' else 37
    print('{"usage_used": %d}' % usage)
elif stage == 'test':
    raise SystemExit(Path('src/app.txt').read_text(encoding='utf-8') != 'fixed\\n')
elif stage == 'build':
    pass
elif stage == 'deploy':
    (deployment / 'pending').write_text(
        os.environ['AGENTIC_MESH_RECOVERY_CANDIDATE_COMMIT'], encoding='utf-8'
    )
elif stage == 'restart':
    shutil.copyfile(deployment / 'pending', deployment / 'revision')
elif stage == 'verify':
    pass
elif stage == 'verify-fail':
    raise SystemExit(23)
elif stage == 'revision':
    print((deployment / 'revision').read_text(encoding='utf-8'))
elif stage == 'rollback':
    (deployment / 'pending').write_text(
        os.environ['AGENTIC_MESH_RECOVERY_PREVIOUS_COMMIT'], encoding='utf-8'
    )
elif stage == 'rollback-fail':
    raise SystemExit(41)
elif stage == 'rollback-restart':
    shutil.copyfile(deployment / 'pending', deployment / 'revision')
elif stage == 'rollback-verify':
    expected = os.environ['AGENTIC_MESH_RECOVERY_PREVIOUS_COMMIT']
    raise SystemExit((deployment / 'revision').read_text(encoding='utf-8') != expected)
else:
    raise SystemExit(99)
""",
        encoding="utf-8",
    )

    def command(stage: str) -> list[str]:
        return [sys.executable, str(helper), stage, str(deployment)]

    plan_value = {
        "schema_version": 1,
        "project_id": "mesh-project",
        "repository": {
            "source": str(source.resolve()),
            "workspace_root": str(workspace.resolve()),
            "base_revision": base,
            "remote": "origin",
            "github_repository": "example/mesh",
            "pr_base": "develop",
        },
        "allowed_paths": ["src/"],
        "evidence_root": str(evidence.resolve()),
        "commands": {
            "repair": command(repair),
            "test": [command("test")],
            "build": [command("build")],
            "deploy": [command("deploy")],
            "restart": [command("restart")],
            "verify": [command(verify)],
            "deployment_revision": [command("revision")],
            "rollback": [command(rollback)],
            "rollback_restart": [command("rollback-restart")],
            "rollback_verify": [command("rollback-verify")],
        },
        "environments": {
            "repair": [],
            "test": [],
            "build": [],
            "deploy": [],
            "verify": [],
            "rollback": [],
            "source_control": [],
        },
        "pull_request": {
            "title": "Recovery candidate",
            "body": "Review and merge through normal release governance.",
        },
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_value), encoding="utf-8")
    job = {
        "project_id": "mesh-project",
        "recovery_request_id": "request-1",
        "incident_id": "incident-1",
        "work_item_id": "work-1",
        "run_id": "run-1",
        "exact_goal": "Repair the broken fixture and leave it working.",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "usage_limit": 1000,
        "tool_profile_reference": "tool-profile/recovery@0.1.0",
        "tool_profile_digest": "a" * 64,
        "image": "agentic-mesh/worker-recovery:0.1.0",
        "mount_references": ["fixture-source"],
        "credential_references": ["fixture-git"],
    }
    return plan_path, plan_value, job, source, remote, deployment, evidence


def _evidence(result: dict[str, object], root: Path) -> dict[str, object]:
    digest = str(result["verification_ref"]).rsplit("/", 1)[-1]
    return json.loads((root / "manifests" / f"{digest}.json").read_text("utf-8"))


def test_recovery_repairs_deploys_pushes_and_creates_review_only_pr(tmp_path: Path):
    plan_path, _, job, source, remote, deployment, evidence_root = _fixture(tmp_path)
    pull_requests = RecordingPullRequests()
    controller = control.RecoveryController(
        control.load_plan(plan_path), pull_requests=pull_requests
    )

    result = controller.execute(job)

    assert result["outcome"] == "succeeded"
    evidence = _evidence(result, evidence_root)
    assert evidence["status"] == "succeeded"
    assert evidence["usage_used"] == 37
    assert result["usage_used"] == 37
    assert evidence["candidate_commit"] == evidence["deployed_commit"]
    assert evidence["previous_deployed_commit"] == evidence["base_commit"]
    assert deployment.joinpath("revision").read_text("utf-8") == evidence[
        "candidate_commit"
    ]
    assert evidence["pull_request_url"] == "https://github.com/example/mesh/pull/17"
    branch = str(evidence["branch"])
    pushed = _run("git", "--git-dir", str(remote), "rev-parse", branch, cwd=tmp_path)
    assert pushed == evidence["candidate_commit"]
    assert len(pull_requests.calls) == 1
    assert not hasattr(control.GitHubCliPullRequests(), "merge")
    assert not hasattr(control.GitHubCliPullRequests(), "approve")
    assert _run("git", "status", "--porcelain", cwd=source) == ""

    replay = controller.execute(job)
    assert replay == result
    assert len(pull_requests.calls) == 1

    second_job = {**job, "run_id": "run-2", "recovery_request_id": "request-2"}
    second_result = controller.execute(second_job)
    second_evidence = _evidence(second_result, evidence_root)
    assert len(second_evidence["stages"]) == len(evidence["stages"])
    assert len(pull_requests.calls) == 2

    digest = str(result["verification_ref"]).rsplit("/", 1)[-1]
    manifest_path = evidence_root / "manifests" / f"{digest}.json"
    manifest_path.write_text("{}", encoding="utf-8")
    with pytest.raises(control.RecoveryControlError, match="digest"):
        controller.execute(job)


def test_unrelated_change_is_rejected_before_deployment_or_push(tmp_path: Path):
    plan_path, _, job, _, remote, deployment, evidence_root = _fixture(
        tmp_path, repair="repair-unrelated"
    )
    pull_requests = RecordingPullRequests()

    result = control.RecoveryController(
        control.load_plan(plan_path), pull_requests=pull_requests
    ).execute(job)

    assert result["outcome"] == "failed"
    evidence = _evidence(result, evidence_root)
    assert "unrelated path" in evidence["error"]
    assert deployment.joinpath("revision").read_text("utf-8") == evidence["base_commit"]
    assert pull_requests.calls == []
    assert (
        _run("git", "--git-dir", str(remote), "branch", "--list", cwd=tmp_path)
        == "develop"
    )


def test_usage_limit_stops_candidate_before_test_or_deployment(tmp_path: Path):
    plan_path, _, job, _, _, deployment, evidence_root = _fixture(
        tmp_path, repair="repair-over-limit"
    )
    job["usage_limit"] = 1000

    result = control.RecoveryController(
        control.load_plan(plan_path), pull_requests=RecordingPullRequests()
    ).execute(job)

    assert result["outcome"] == "failed"
    assert result["usage_used"] == 1001
    evidence = _evidence(result, evidence_root)
    assert "usage limit" in evidence["error"]
    assert deployment.joinpath("revision").read_text("utf-8") == evidence["base_commit"]


def test_failed_verification_restores_and_verifies_previous_commit(tmp_path: Path):
    plan_path, _, job, _, _, deployment, evidence_root = _fixture(
        tmp_path, verify="verify-fail"
    )
    pull_requests = RecordingPullRequests()

    result = control.RecoveryController(
        control.load_plan(plan_path), pull_requests=pull_requests
    ).execute(job)

    assert result["outcome"] == "failed"
    evidence = _evidence(result, evidence_root)
    assert evidence["rolled_back"] is True
    assert evidence["rollback_verified"] is True
    assert deployment.joinpath("revision").read_text("utf-8") == evidence[
        "previous_deployed_commit"
    ]
    assert pull_requests.calls == []


def test_failed_rollback_is_visible_and_never_reports_success(tmp_path: Path):
    plan_path, _, job, _, _, deployment, evidence_root = _fixture(
        tmp_path, verify="verify-fail", rollback="rollback-fail"
    )

    result = control.RecoveryController(
        control.load_plan(plan_path), pull_requests=RecordingPullRequests()
    ).execute(job)

    assert result["outcome"] == "failed"
    evidence = _evidence(result, evidence_root)
    assert evidence["rolled_back"] is False
    assert evidence["rollback_verified"] is False
    assert "rollback failed" in evidence["error"]
    assert deployment.joinpath("revision").read_text("utf-8") == evidence[
        "candidate_commit"
    ]


@pytest.mark.parametrize(
    "unsafe",
    [
        ["gh", "pr", "merge", "17"],
        ["git", "push", "origin", "main"],
        ["git", "status", "--short"],
        ["sh", "-c", "gh pr merge 17"],
    ],
)
def test_plan_rejects_governance_and_shell_commands(tmp_path: Path, unsafe: list[str]):
    plan_path, plan, *_ = _fixture(tmp_path)
    plan["commands"]["build"] = [unsafe]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(control.PlanError):
        control.load_plan(plan_path)


def test_plan_rejects_embedded_credential_values(tmp_path: Path):
    plan_path, plan, *_ = _fixture(tmp_path)
    plan["credentials"] = {"GH_TOKEN": "secret-value"}
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(control.PlanError):
        control.load_plan(plan_path)


def test_plan_keeps_source_control_credentials_out_of_agent_stages(tmp_path: Path):
    plan_path, plan, *_ = _fixture(tmp_path)
    plan["environments"]["source_control"] = ["GH_TOKEN"]
    plan["environments"]["repair"] = ["GH_TOKEN"]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(control.PlanError, match="credential references"):
        control.load_plan(plan_path)


@pytest.mark.parametrize("root_name", ["workspace_root", "evidence_root"])
def test_plan_requires_runtime_roots_outside_source(tmp_path: Path, root_name: str):
    plan_path, plan, *_ = _fixture(tmp_path)
    source = Path(plan["repository"]["source"])
    if root_name == "workspace_root":
        plan["repository"][root_name] = str(source / "runtime")
    else:
        plan[root_name] = str(source / "runtime")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(control.PlanError, match="roots must be separate"):
        control.load_plan(plan_path)


def test_recovery_image_installs_control_runner():
    dockerfile = (ROOT / "docker" / "v5" / "Dockerfile").read_text("utf-8")
    manifest = json.loads(
        (ROOT / "docker" / "v5" / "manifests" / "recovery.json").read_text("utf-8")
    )
    dockerignore = (ROOT / "docker" / "v5" / ".dockerignore").read_text("utf-8")
    assert (
        "recovery-control.py /usr/local/bin/agentic-mesh-recovery-control"
        in dockerfile
    )
    assert "agentic-mesh-recovery-control" in manifest["required_commands"]
    assert "!recovery-control.py" in dockerignore.splitlines()
