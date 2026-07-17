from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from pathlib import Path
import threading

import pytest

from agentic_mesh_v5 import config_activation
from agentic_mesh_v5.cli import main
from agentic_mesh_v5.config_activation import ConfigActivationError
from agentic_mesh_v5.config_activation import ConfigActivationStore


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _repository(tmp_path: Path) -> Path:
    schema = tmp_path / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    _package(tmp_path, "1.0.0", workers=1)
    _package(tmp_path, "2.0.0", workers=2)
    _package(tmp_path, "3.0.0", workers=3)
    return tmp_path


def _package(root: Path, version: str, *, workers: int) -> None:
    package_root = root / "packages" / "system" / "core" / version
    package_root.mkdir(parents=True)
    (package_root / "settings.json").write_text(
        json.dumps({"workers": workers}, sort_keys=True) + "\n", encoding="utf-8"
    )
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "core",
                "kind": "system",
                "version": version,
                "content": ["settings.json"],
                "dependencies": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _store(root: Path) -> ConfigActivationStore:
    return ConfigActivationStore(root, clock=lambda: NOW)


def _release(store: ConfigActivationStore, root: Path, version: str, actor: str = "pm"):
    assert store.repository_root == root.resolve()
    return store.create_release([f"system/core@{version}"], actor=actor)


def test_invalid_draft_creates_no_release_or_activation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    store = _store(root)

    with pytest.raises(ConfigActivationError, match="package not found"):
        _release(store, root, "9.0.0")

    assert not store.releases_dir.exists()
    assert not store.activation_path.exists()
    assert store.get_state().revision == 0


def test_release_is_immutable_and_idempotent(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    store = _store(root)

    first = _release(store, root, "1.0.0", actor="alice")
    repeated = _release(store, root, "1.0.0", actor="bob")

    assert repeated == first
    assert repeated.created_by == "alice"
    release_path = store.releases_dir / f"{first.digest}.json"
    assert store.get_release(first.digest) == first
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["resolved"]["settings"]["workers"] = 99
    release_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigActivationError, match="invalid release record"):
        store.get_release(first.digest)


def test_activation_idempotency_and_rollback_are_audited(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    store = _store(root)
    first = _release(store, root, "1.0.0")
    second = _release(store, root, "2.0.0")

    state = store.activate(first.digest, actor="pm", expected_active=None)
    assert state.revision == 1
    assert store.activate(first.digest, actor="pm") == state
    state = store.activate(
        second.digest,
        actor="pm",
        reason="promote tested draft",
        expected_active=first.digest,
    )
    state = store.rollback(first.digest, actor="pm", reason="failed smoke test")

    assert state.revision == 3
    assert state.active_digest == first.digest
    assert [event["action"] for event in state.history] == [
        "activate",
        "activate",
        "rollback",
    ]
    assert state.history[-1] == {
        "revision": 3,
        "action": "rollback",
        "actor": "pm",
        "reason": "failed smoke test",
        "previous_digest": second.digest,
        "target_digest": first.digest,
        "timestamp": "2026-07-17T12:00:00Z",
    }


def test_concurrent_expected_current_activation_has_one_winner(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    store = _store(root)
    first = _release(store, root, "1.0.0")
    candidates = [_release(store, root, version) for version in ("2.0.0", "3.0.0")]
    store.activate(first.digest, actor="pm", expected_active=None)
    stores = [_store(root), _store(root)]
    barrier = threading.Barrier(2)

    def activate(request: tuple[ConfigActivationStore, str]) -> str:
        candidate_store, digest = request
        barrier.wait()
        try:
            candidate_store.activate(
                digest, actor=digest[:8], expected_active=first.digest
            )
        except ConfigActivationError:
            return "rejected"
        return "activated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                activate,
                zip(stores, [item.digest for item in candidates], strict=True),
            )
        )

    assert sorted(outcomes) == ["activated", "rejected"]
    state = store.get_state()
    assert state.revision == 2
    assert state.active_digest in {item.digest for item in candidates}


def test_failure_before_atomic_replace_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    store = _store(root)
    first = _release(store, root, "1.0.0")
    second = _release(store, root, "2.0.0")
    before = store.activate(first.digest, actor="pm", expected_active=None)

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated commit failure")

    monkeypatch.setattr(config_activation.os, "replace", fail_replace)
    with pytest.raises(ConfigActivationError, match="atomic write failed"):
        store.activate(second.digest, actor="pm", expected_active=first.digest)

    assert store.get_state() == before
    assert not [
        path
        for path in store.activation_path.parent.iterdir()
        if path.name.endswith(".tmp")
    ]


def test_release_cli_creates_activates_and_reports_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    code = main(
        [
            "--json",
            "release-create",
            "--config-root",
            str(root),
            "--package",
            "system/core@1.0.0",
            "--actor",
            "pm",
        ]
    )
    created = json.loads(capsys.readouterr().out)
    assert code == 0
    assert created["status"] == "release-created"

    code = main(
        [
            "--json",
            "release-activate",
            "--config-root",
            str(root),
            "--digest",
            created["digest"],
            "--actor",
            "pm",
            "--expect-empty",
        ]
    )
    active = json.loads(capsys.readouterr().out)
    assert code == 0
    assert active["status"] == "release-active"
    assert active["active_digest"] == created["digest"]

    code = main(
        ["--json", "release-status", "--config-root", str(root)]
    )
    status = json.loads(capsys.readouterr().out)
    assert code == 0
    assert status["revision"] == 1
    assert status["active_digest"] == created["digest"]
