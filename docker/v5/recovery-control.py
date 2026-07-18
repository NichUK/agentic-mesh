#!/usr/bin/env python3
"""Bounded repair/deploy runner for the independent V5 recovery image."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Mapping, Protocol, Sequence


_SAFE_ENVIRONMENT = {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP"}
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_STAGES = (
    "repair",
    "test",
    "build",
    "deploy",
    "restart",
    "verify",
    "deployment_revision",
    "rollback",
    "rollback_restart",
    "rollback_verify",
)
_ENVIRONMENT_STAGES = (
    "repair",
    "test",
    "build",
    "deploy",
    "verify",
    "rollback",
    "source_control",
)


class RecoveryControlError(RuntimeError):
    pass


class PlanError(RecoveryControlError):
    pass


class CommandFailed(RecoveryControlError):
    def __init__(self, stage: str, exit_code: int) -> None:
        super().__init__(f"{stage} command failed with exit code {exit_code}")
        self.stage = stage
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class RepositoryPlan:
    source: Path
    workspace_root: Path
    base_revision: str
    remote: str
    github_repository: str
    pr_base: str


@dataclass(frozen=True, slots=True)
class PullRequestPlan:
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    project_id: str
    repository: RepositoryPlan
    allowed_paths: tuple[str, ...]
    evidence_root: Path
    commands: Mapping[str, tuple[tuple[str, ...], ...]]
    environments: Mapping[str, tuple[str, ...]]
    pull_request: PullRequestPlan
    digest: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    exit_code: int
    output_digest: str


class PullRequests(Protocol):
    def create(
        self,
        *,
        cwd: Path,
        repository: str,
        base: str,
        head: str,
        title: str,
        body: str,
        environment: Mapping[str, str],
        timeout: float,
    ) -> str: ...


class GitHubCliPullRequests:
    """Create-only GitHub boundary; approval and merge are deliberately absent."""

    def create(
        self,
        *,
        cwd: Path,
        repository: str,
        base: str,
        head: str,
        title: str,
        body: str,
        environment: Mapping[str, str],
        timeout: float,
    ) -> str:
        result = _execute(
            (
                "gh", "pr", "create", "--repo", repository, "--base", base,
                "--head", head, "--title", title, "--body", body,
            ),
            cwd=cwd,
            environment=environment,
            timeout=timeout,
            input_text=None,
        )
        if result.exit_code:
            raise CommandFailed("pull_request", result.exit_code)
        url = result.stdout.strip()
        if not re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[0-9]+", url):
            raise RecoveryControlError("pull_request command returned an invalid URL")
        return url


def load_plan(path: Path) -> RecoveryPlan:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError("recovery plan must be valid UTF-8 JSON") from exc
    root = _mapping(
        value,
        "plan",
        {"schema_version", "project_id", "repository", "allowed_paths",
         "evidence_root", "commands", "environments", "pull_request"},
    )
    if root["schema_version"] != 1:
        raise PlanError("recovery plan schema_version must be 1")
    project_id = _identifier(root["project_id"], "project_id")
    repository_value = _mapping(
        root["repository"],
        "repository",
        {"source", "workspace_root", "base_revision", "remote",
         "github_repository", "pr_base"},
    )
    repository = RepositoryPlan(
        source=_absolute_path(repository_value["source"], "repository.source"),
        workspace_root=_absolute_path(
            repository_value["workspace_root"], "repository.workspace_root"
        ),
        base_revision=_text(repository_value["base_revision"], "base_revision", 256),
        remote=_identifier(repository_value["remote"], "remote"),
        github_repository=_github_repository(repository_value["github_repository"]),
        pr_base=_text(repository_value["pr_base"], "pr_base", 256),
    )
    paths = root["allowed_paths"]
    if not isinstance(paths, list) or not paths:
        raise PlanError("allowed_paths must be a non-empty array")
    allowed_paths = tuple(_allowed_path(item) for item in paths)
    if len(set(allowed_paths)) != len(allowed_paths):
        raise PlanError("allowed_paths contains duplicates")
    commands_value = _mapping(root["commands"], "commands", set(_STAGES))
    commands: dict[str, tuple[tuple[str, ...], ...]] = {}
    for stage in _STAGES:
        commands[stage] = _commands(commands_value[stage], stage)
    environments_value = _mapping(
        root["environments"], "environments", set(_ENVIRONMENT_STAGES)
    )
    environments = {
        stage: _environment_names(environments_value[stage], stage)
        for stage in _ENVIRONMENT_STAGES
    }
    source_control_names = set(environments["source_control"])
    exposed = {
        stage: sorted(source_control_names.intersection(names))
        for stage, names in environments.items()
        if stage != "source_control" and source_control_names.intersection(names)
    }
    if exposed:
        raise PlanError(
            "source-control credential references must be isolated from repair and "
            "deployment stages"
        )
    pull_request_value = _mapping(
        root["pull_request"], "pull_request", {"title", "body"}
    )
    pull_request = PullRequestPlan(
        title=_text(pull_request_value["title"], "pull_request.title", 256),
        body=_text(pull_request_value["body"], "pull_request.body", 4000),
    )
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return RecoveryPlan(
        project_id=project_id,
        repository=repository,
        allowed_paths=allowed_paths,
        evidence_root=_absolute_path(root["evidence_root"], "evidence_root"),
        commands=commands,
        environments=environments,
        pull_request=pull_request,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


class RecoveryController:
    def __init__(
        self, plan: RecoveryPlan, pull_requests: PullRequests | None = None
    ) -> None:
        self.plan = plan
        self.pull_requests = pull_requests or GitHubCliPullRequests()
        self.stages: list[dict[str, object]] = []

    def execute(self, job: Mapping[str, object]) -> dict[str, object]:
        job = _validated_job(job, self.plan.project_id)
        run_id = str(job["run_id"])
        run_key = _digest(run_id)
        result_path = self.plan.evidence_root / "results" / f"{run_key}.json"
        if result_path.is_file():
            return _load_completed_result(result_path)
        deadline = _deadline(str(job["deadline_at"]))
        branch = _branch_name(run_id)
        workspace = self.plan.repository.workspace_root / run_key
        evidence: dict[str, object] = {
            "schema_version": 1,
            "project_id": self.plan.project_id,
            "run_id": run_id,
            "goal_digest": _digest(str(job["exact_goal"])),
            "plan_digest": self.plan.digest,
            "status": "failed",
            "branch": branch,
            "base_commit": None,
            "candidate_commit": None,
            "previous_deployed_commit": None,
            "deployed_commit": None,
            "pull_request_url": None,
            "rolled_back": False,
            "rollback_verified": False,
            "usage_used": 0,
            "stages": self.stages,
            "error": None,
            "completed_at": None,
        }
        deploy_started = False
        success = False
        worktree_added = False
        try:
            self._validate_source()
            base_commit = self._git(
                self.plan.repository.source,
                "rev-parse",
                "--verify",
                f"{self.plan.repository.base_revision}^{{commit}}",
            ).stdout.strip()
            if _COMMIT.fullmatch(base_commit) is None:
                raise RecoveryControlError("base revision did not resolve to a commit")
            evidence["base_commit"] = base_commit
            if workspace.exists():
                raise RecoveryControlError("recovery workspace already exists")
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self._git(
                self.plan.repository.source,
                "worktree", "add", "--detach", str(workspace), base_commit,
            )
            worktree_added = True
            self._git(workspace, "switch", "--create", branch)

            repair_results = self._stage_commands(
                "repair", workspace, job, deadline, extra_environment={}
            )
            usage_used = _reported_usage(repair_results[-1].stdout)
            evidence["usage_used"] = usage_used
            if usage_used > int(job["usage_limit"]):
                raise RecoveryControlError(
                    "repair command exceeded the recovery usage limit"
                )
            self._validate_changes(workspace, require_changes=True)
            self._stage_commands("test", workspace, job, deadline, extra_environment={})
            self._validate_changes(workspace, require_changes=True)
            self._git(workspace, "add", "--all")
            commit_environment = self._environment("source_control")
            commit_environment.update(
                {
                    "GIT_AUTHOR_NAME": "Agentic Mesh Recovery",
                    "GIT_AUTHOR_EMAIL": "recovery@agentic-mesh.invalid",
                    "GIT_COMMITTER_NAME": "Agentic Mesh Recovery",
                    "GIT_COMMITTER_EMAIL": "recovery@agentic-mesh.invalid",
                }
            )
            self._git(
                workspace,
                "commit", "--message", f"Repair Agentic Mesh recovery {run_id}",
                environment=commit_environment,
            )
            candidate_commit = self._git(workspace, "rev-parse", "HEAD").stdout.strip()
            if _COMMIT.fullmatch(candidate_commit) is None:
                raise RecoveryControlError("candidate commit is invalid")
            evidence["candidate_commit"] = candidate_commit
            self._require_clean(workspace)

            context = {"AGENTIC_MESH_RECOVERY_CANDIDATE_COMMIT": candidate_commit}
            self._stage_commands("build", workspace, job, deadline, context)
            self._require_clean(workspace)
            previous = self._single_revision(workspace, job, deadline, context)
            evidence["previous_deployed_commit"] = previous
            context["AGENTIC_MESH_RECOVERY_PREVIOUS_COMMIT"] = previous

            deploy_started = True
            self._stage_commands("deploy", workspace, job, deadline, context)
            self._stage_commands("restart", workspace, job, deadline, context)
            self._stage_commands("verify", workspace, job, deadline, context)
            deployed = self._single_revision(workspace, job, deadline, context)
            if deployed != candidate_commit:
                raise RecoveryControlError(
                    "running deployment does not match the candidate commit"
                )
            evidence["deployed_commit"] = deployed
            self._require_clean(workspace)

            source_environment = self._environment("source_control")
            self._git(
                workspace,
                "push", self.plan.repository.remote, f"HEAD:refs/heads/{branch}",
                environment=source_environment,
            )
            self._record_synthetic_stage("push", branch)
            pr_url = self.pull_requests.create(
                cwd=workspace,
                repository=self.plan.repository.github_repository,
                base=self.plan.repository.pr_base,
                head=branch,
                title=self.plan.pull_request.title,
                body=self.plan.pull_request.body,
                environment=source_environment,
                timeout=_remaining(deadline),
            )
            evidence["pull_request_url"] = pr_url
            self._record_synthetic_stage("pull_request", pr_url)
            evidence["status"] = "succeeded"
            success = True
        except (
            OSError,
            UnicodeError,
            subprocess.SubprocessError,
            RecoveryControlError,
        ) as exc:
            evidence["error"] = _safe_error(exc)
            if deploy_started and evidence["previous_deployed_commit"]:
                try:
                    context = {
                        "AGENTIC_MESH_RECOVERY_CANDIDATE_COMMIT": str(
                            evidence["candidate_commit"]
                        ),
                        "AGENTIC_MESH_RECOVERY_PREVIOUS_COMMIT": str(
                            evidence["previous_deployed_commit"]
                        ),
                    }
                    self._stage_commands("rollback", workspace, job, deadline, context)
                    evidence["rolled_back"] = True
                    self._stage_commands(
                        "rollback_restart", workspace, job, deadline, context
                    )
                    self._stage_commands(
                        "rollback_verify", workspace, job, deadline, context
                    )
                    restored = self._single_revision(workspace, job, deadline, context)
                    if restored != evidence["previous_deployed_commit"]:
                        raise RecoveryControlError(
                            "rollback did not restore the previous deployed commit"
                        )
                    evidence["rollback_verified"] = True
                except (
                    OSError,
                    UnicodeError,
                    subprocess.SubprocessError,
                    RecoveryControlError,
                ) as rollback:
                    evidence["error"] = (
                        f"{evidence['error']}; rollback failed: {_safe_error(rollback)}"
                    )[:1000]
        finally:
            evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
            try:
                result = self._write_evidence(evidence, result_path)
            finally:
                if worktree_added:
                    self._git(
                        self.plan.repository.source,
                        "worktree", "remove", "--force", str(workspace),
                        check=False,
                    )
                if not success:
                    self._git(
                        self.plan.repository.source,
                        "branch", "--delete", "--force", branch,
                        check=False,
                    )
        return result

    def _validate_source(self) -> None:
        source = self.plan.repository.source
        if not source.is_dir():
            raise RecoveryControlError("source repository does not exist")
        root = self._git(source, "rev-parse", "--show-toplevel").stdout.strip()
        if Path(root).resolve() != source.resolve():
            raise RecoveryControlError("source path is not the repository root")
        self._require_clean(source)

    def _validate_changes(self, workspace: Path, *, require_changes: bool) -> None:
        changed: set[str] = set()
        for arguments in (
            ("diff", "--name-only", "-z"),
            ("diff", "--cached", "--name-only", "-z"),
            ("ls-files", "--others", "--exclude-standard", "-z"),
        ):
            changed.update(
                item
                for item in self._git(workspace, *arguments).stdout.split("\0")
                if item
            )
        if require_changes and not changed:
            raise RecoveryControlError("repair command made no source change")
        for item in sorted(changed):
            normalized = _allowed_path(item)
            if not any(
                normalized == allowed.rstrip("/")
                or (allowed.endswith("/") and normalized.startswith(allowed))
                for allowed in self.plan.allowed_paths
            ):
                raise RecoveryControlError(
                    f"repair changed unrelated path: {normalized}"
                )
            path = workspace / Path(*PurePosixPath(normalized).parts)
            if path.is_symlink():
                raise RecoveryControlError(
                    f"repair created or changed a symlink: {normalized}"
                )

    def _require_clean(self, workspace: Path) -> None:
        if self._git(
            workspace, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout:
            raise RecoveryControlError(
                "repository is not clean at a protected boundary"
            )

    def _single_revision(
        self,
        workspace: Path,
        job: Mapping[str, object],
        deadline: datetime,
        context: Mapping[str, str],
    ) -> str:
        results = self._stage_commands(
            "deployment_revision", workspace, job, deadline, context
        )
        revision = results[-1].stdout.strip()
        if _COMMIT.fullmatch(revision) is None:
            raise RecoveryControlError(
                "deployment revision command returned an invalid commit"
            )
        return revision

    def _stage_commands(
        self,
        stage: str,
        workspace: Path,
        job: Mapping[str, object],
        deadline: datetime,
        extra_environment: Mapping[str, str],
    ) -> tuple[CommandResult, ...]:
        environment_stage = (
            "rollback" if stage.startswith("rollback") else
            "deploy" if stage == "restart" else
            "verify" if stage in {"verify", "deployment_revision"} else stage
        )
        environment = self._environment(environment_stage)
        environment.update(extra_environment)
        environment.update(
            {
                "AGENTIC_MESH_RECOVERY_PROJECT_ID": str(job["project_id"]),
                "AGENTIC_MESH_RECOVERY_RUN_ID": str(job["run_id"]),
                "AGENTIC_MESH_RECOVERY_USAGE_LIMIT": str(job["usage_limit"]),
            }
        )
        input_text = json.dumps(job, sort_keys=True) if stage == "repair" else None
        results: list[CommandResult] = []
        for argv in self.plan.commands[stage]:
            result = _execute(
                argv,
                cwd=workspace,
                environment=environment,
                timeout=_remaining(deadline),
                input_text=input_text,
            )
            self.stages.append(
                {
                    "stage": stage,
                    "command_digest": _digest(json.dumps(argv)),
                    "exit_code": result.exit_code,
                    "output_digest": result.output_digest,
                }
            )
            if result.exit_code:
                raise CommandFailed(stage, result.exit_code)
            results.append(result)
        return tuple(results)

    def _environment(self, stage: str) -> dict[str, str]:
        result = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _SAFE_ENVIRONMENT
        }
        for name in self.plan.environments[stage]:
            if name not in os.environ:
                raise RecoveryControlError(
                    f"required {stage} environment reference is unavailable: {name}"
                )
            result[name] = os.environ[name]
        return result

    def _git(
        self,
        cwd: Path,
        *arguments: str,
        environment: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        result = _execute(
            ("git", *arguments),
            cwd=cwd,
            environment=environment or self._environment("source_control"),
            timeout=120,
            input_text=None,
        )
        if check and result.exit_code:
            raise CommandFailed("git", result.exit_code)
        return result

    def _record_synthetic_stage(self, stage: str, value: str) -> None:
        self.stages.append(
            {
                "stage": stage,
                "command_digest": None,
                "exit_code": 0,
                "output_digest": _digest(value),
            }
        )

    def _write_evidence(
        self, evidence: Mapping[str, object], result_path: Path
    ) -> dict[str, object]:
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        digest = hashlib.sha256(encoded).hexdigest()
        evidence_directory = self.plan.evidence_root / "manifests"
        evidence_directory.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_directory / f"{digest}.json"
        try:
            with evidence_path.open("xb") as handle:
                handle.write(encoded)
        except FileExistsError:
            if evidence_path.read_bytes() != encoded:
                raise RecoveryControlError("content-addressed evidence collision")
        result = {
            "outcome": "succeeded" if evidence["status"] == "succeeded" else "failed",
            "safe_summary": (
                "Recovery candidate was verified and a review-only pull request "
                "was created."
                if evidence["status"] == "succeeded"
                else "Recovery candidate did not pass all controls; inspect "
                "immutable evidence."
            ),
            "usage_used": evidence["usage_used"],
            "verification_ref": (
                f"evidence://recovery/{self.plan.project_id}/"
                f"{evidence['run_id']}/{digest}"
            ),
        }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            result, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        try:
            with result_path.open("xb") as handle:
                handle.write(payload)
        except FileExistsError:
            if result_path.read_bytes() != payload:
                raise RecoveryControlError(
                    "recovery run already has a different result"
                )
        return result


def _execute(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
    input_text: str | None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(environment),
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecoveryControlError(
            "recovery command reached the exact job deadline"
        ) from exc
    output = (completed.stdout + "\n" + completed.stderr).encode("utf-8")
    return CommandResult(
        stdout=completed.stdout,
        exit_code=completed.returncode,
        output_digest=hashlib.sha256(output).hexdigest(),
    )


def _validated_job(value: Mapping[str, object], project_id: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RecoveryControlError("recovery job must be an object")
    required = {
        "project_id", "recovery_request_id", "incident_id", "work_item_id",
        "run_id", "exact_goal", "deadline_at", "usage_limit",
        "tool_profile_reference", "tool_profile_digest", "image",
        "mount_references", "credential_references",
    }
    if set(value) != required:
        raise RecoveryControlError("recovery job contract is invalid")
    result = dict(value)
    if _identifier(result["project_id"], "project_id") != project_id:
        raise RecoveryControlError("recovery job project does not match the plan")
    _identifier(result["run_id"], "run_id")
    _text(result["exact_goal"], "exact_goal", 8000)
    if type(result["usage_limit"]) is not int or int(result["usage_limit"]) <= 0:
        raise RecoveryControlError("recovery usage limit is invalid")
    _deadline(str(result["deadline_at"]))
    return result


def _load_completed_result(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryControlError("completed recovery result is unreadable") from exc
    if not isinstance(value, dict) or set(value) != {
        "outcome", "safe_summary", "usage_used", "verification_ref"
    }:
        raise RecoveryControlError("completed recovery result is invalid")
    return value


def _reported_usage(stdout: str) -> int:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RecoveryControlError(
            "repair command must report provider usage as JSON"
        ) from exc
    if (
        not isinstance(value, Mapping)
        or set(value) != {"usage_used"}
        or type(value["usage_used"]) is not int
        or value["usage_used"] < 0
    ):
        raise RecoveryControlError("repair command usage report is invalid")
    return value["usage_used"]


def _mapping(value: object, label: str, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise PlanError(f"{label} must contain exactly: {', '.join(sorted(keys))}")
    return value


def _commands(value: object, stage: str) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"commands.{stage} must be a non-empty array")
    if stage == "repair" and value and isinstance(value[0], str):
        value = [value]
    result: list[tuple[str, ...]] = []
    for item in value:
        if not isinstance(item, list) or not item or any(
            not isinstance(part, str) or not part for part in item
        ):
            raise PlanError(f"commands.{stage} must contain argv arrays")
        _reject_unsafe_command(item, stage)
        result.append(tuple(item))
    return tuple(result)


def _reject_unsafe_command(argv: Sequence[str], stage: str) -> None:
    executable = Path(argv[0]).name.lower()
    if executable in {"gh", "gh.exe"}:
        raise PlanError(f"commands.{stage} cannot invoke GitHub CLI")
    if executable in {"sh", "bash", "cmd", "cmd.exe", "powershell", "pwsh"}:
        lowered = {item.lower() for item in argv[1:]}
        if lowered.intersection({"-c", "/c", "-command", "-encodedcommand"}):
            raise PlanError(f"commands.{stage} cannot execute a shell command string")
    if executable in {"git", "git.exe"} and len(argv) > 1 and argv[1].lower() in {
        "push", "commit", "merge", "rebase", "tag",
    }:
        raise PlanError(f"commands.{stage} cannot mutate source-control governance")


def _environment_names(value: object, stage: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or _ENVIRONMENT_NAME.fullmatch(item) is None
        for item in value
    ):
        raise PlanError(f"environments.{stage} must contain environment variable names")
    if len(set(value)) != len(value):
        raise PlanError(f"environments.{stage} contains duplicates")
    return tuple(value)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PlanError(f"{label} is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PlanError(f"{label} is invalid")
    return value.strip()


def _absolute_path(value: object, label: str) -> Path:
    text = _text(value, label, 1000)
    path = Path(text)
    if not path.is_absolute():
        raise PlanError(f"{label} must be absolute")
    return path


def _github_repository(value: object) -> str:
    text = _text(value, "github_repository", 256)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text) is None:
        raise PlanError("github_repository is invalid")
    return text


def _allowed_path(value: object) -> str:
    text = _text(value, "allowed_path", 500).replace("\\", "/")
    trailing = text.endswith("/")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PlanError("allowed_path must stay within the repository")
    normalized = path.as_posix()
    if normalized == ".git" or normalized.startswith(".git/"):
        raise PlanError("allowed_path cannot include Git metadata")
    return normalized + ("/" if trailing else "")


def _deadline(value: str) -> datetime:
    timestamp = value.strip().replace(" ", "T", 1)
    if re.search(r"[+-][0-9]{2}$", timestamp):
        timestamp += ":00"
    try:
        result = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise RecoveryControlError("recovery deadline is invalid") from exc
    if result.tzinfo is None:
        raise RecoveryControlError("recovery deadline must include a time zone")
    return result


def _remaining(deadline: datetime) -> float:
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise RecoveryControlError("recovery job deadline has elapsed")
    return max(0.1, remaining)


def _branch_name(run_id: str) -> str:
    return f"codex/recovery-{hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:12]}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:1000]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        result = RecoveryController(load_plan(args.plan)).execute(job)
    except (UnicodeError, json.JSONDecodeError, RecoveryControlError) as exc:
        result = {
            "outcome": "failed",
            "safe_summary": "Recovery controls rejected the execution request.",
            "usage_used": 0,
            "verification_ref": (
                f"recovery-control://rejected/{_digest(_safe_error(exc))}"
            ),
        }
    sys.stdout.write(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
