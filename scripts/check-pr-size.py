from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from dataclasses import dataclass


class GitCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiffSize:
    files: int
    added: int
    deleted: int

    @property
    def changed_lines(self) -> int:
        return self.added + self.deleted


@dataclass
class FileChange:
    added: int = 0
    deleted: int = 0


def git_diff_numstat(*args: str) -> str:
    command = ["git", "diff", "--numstat", "--find-renames", *args]
    return run_git_command(command)


def git_untracked_files() -> list[str]:
    command = ["git", "ls-files", "--others", "--exclude-standard"]
    output = run_git_command(command)
    return [line for line in output.splitlines() if line.strip()]


def run_git_command(command: list[str]) -> str:
    try:
        return subprocess.check_output(
            command,
            text=True,
            encoding="utf-8",
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GitCommandError("git is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(command)
        details = exc.stderr.strip() if exc.stderr else "no git error output"
        raise GitCommandError(f"`{cmd}` failed: {details}") from exc


def added_lines_for_untracked(path: str) -> int:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return 0
    if b"\0" in data:
        return 0
    return len(data.splitlines())


def collect_numstat(changes: dict[str, FileChange], value: str) -> None:
    for line in value.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        path = parts[2]
        change = changes.setdefault(path, FileChange())
        if parts[0] != "-":
            change.added += int(parts[0])
        if parts[1] != "-":
            change.deleted += int(parts[1])


def measure_diff(base: str, *, include_uncommitted: bool) -> DiffSize:
    changes: dict[str, FileChange] = {}
    collect_numstat(changes, git_diff_numstat(f"{base}...HEAD"))
    if include_uncommitted:
        collect_numstat(changes, git_diff_numstat("HEAD"))
        for path in git_untracked_files():
            change = changes.setdefault(path, FileChange())
            change.added += added_lines_for_untracked(path)
    return DiffSize(
        files=len(changes),
        added=sum(change.added for change in changes.values()),
        deleted=sum(change.deleted for change in changes.values()),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail when the current branch is too large for a reviewable PR."
    )
    parser.add_argument("--base", default="origin/develop")
    parser.add_argument("--max-files", type=int, default=50)
    parser.add_argument("--max-lines", type=int, default=5000)
    parser.add_argument("--warn-files", type=int, default=25)
    parser.add_argument("--warn-lines", type=int, default=1500)
    parser.add_argument(
        "--committed-only",
        action="store_true",
        help="Ignore staged, unstaged, and untracked working-tree changes.",
    )
    args = parser.parse_args()

    try:
        size = measure_diff(args.base, include_uncommitted=not args.committed_only)
    except GitCommandError as exc:
        print(f"PR size check could not run: {exc}")
        print(
            "Ensure this command runs inside a git repository and --base points to a valid ref."
        )
        return 2
    print(
        "PR size against "
        f"{args.base}: {size.files} files, {size.added} added, "
        f"{size.deleted} deleted, {size.changed_lines} changed lines"
    )

    failures: list[str] = []
    if size.files > args.max_files:
        failures.append(f"{size.files} files exceeds hard limit {args.max_files}")
    if size.changed_lines > args.max_lines:
        failures.append(
            f"{size.changed_lines} changed lines exceeds hard limit {args.max_lines}"
        )
    if failures:
        print("PR size check failed:")
        for failure in failures:
            print(f"- {failure}")
        print("Split the work into smaller, reviewable PRs before merge.")
        return 1

    warnings: list[str] = []
    if size.files > args.warn_files:
        warnings.append(f"{size.files} files exceeds preferred limit {args.warn_files}")
    if size.changed_lines > args.warn_lines:
        warnings.append(
            f"{size.changed_lines} changed lines exceeds preferred limit {args.warn_lines}"
        )
    if warnings:
        print("PR size warning:")
        for warning in warnings:
            print(f"- {warning}")
        print("Keep this PR only if it is one coherent slice.")
    else:
        print("PR size is within the preferred review range.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
