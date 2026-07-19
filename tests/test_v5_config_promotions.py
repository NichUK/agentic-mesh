from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_mesh_v5.config_activation import ConfigActivationError
from agentic_mesh_v5.config_activation import ConfigActivationStore
from agentic_mesh_v5.config_promotions import ConfigPromotionConflict
from agentic_mesh_v5.config_promotions import ConfigPromotionStore
from agentic_mesh_v5.config_promotions import structural_diff


def _package(root: Path, version: str, workers: int) -> None:
    package = root / "packages" / "system" / "core" / version
    package.mkdir(parents=True)
    (package / "settings.json").write_text(
        json.dumps({"workers": workers}) + "\n", encoding="utf-8"
    )
    (package / "package.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "core",
                "kind": "system",
                "version": version,
                "content": ["settings.json"],
                "dependencies": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def promotion(tmp_path: Path) -> ConfigPromotionStore:
    schema = tmp_path / "schemas" / "package.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    _package(tmp_path, "1.0.0", 1)
    _package(tmp_path, "2.0.0", 2)
    return ConfigPromotionStore(
        ConfigActivationStore(tmp_path), project_id="alpha"
    )


def test_non_sponsor_draft_requires_approval_then_activates_and_rolls_back(
    promotion: ConfigPromotionStore,
) -> None:
    first = promotion.activation.create_release(
        ["system/core@1.0.0"], actor="sponsor"
    )
    promotion.activation.activate(first.digest, actor="sponsor", expected_active=None)
    draft = promotion.create(
        draft_id="draft-1",
        references=["system/core@2.0.0"],
        expected_active_digest=first.digest,
        actor="operator",
    )
    assert promotion.create(
        draft_id="draft-1",
        references=["system/core@2.0.0"],
        expected_active_digest=first.digest,
        actor="operator",
    ) == draft

    validated = promotion.validate(
        draft.draft_id, actor="operator", sponsor_authored=False
    )
    approved = promotion.decide(
        draft.draft_id,
        sponsor_id="sponsor",
        decision="approved",
        rationale="Reviewed effective diff",
    )
    activated = promotion.activate(
        draft.draft_id, actor="operator", reason="Approved promotion"
    )
    rolled_back = promotion.rollback(
        first.digest, sponsor_id="sponsor", reason="Acceptance failed"
    )

    assert draft.status == "draft"
    assert validated.status == "pending_approval"
    assert validated.validation["diff"]
    assert approved.status == "approved"
    assert activated.status == "activated"
    assert promotion.activate(draft.draft_id, actor="operator", reason="replay") == activated
    assert rolled_back.active_digest == first.digest
    assert [event["action"] for event in rolled_back.history] == [
        "activate", "activate", "rollback"
    ]


def test_sponsor_validation_is_implicitly_approved(promotion: ConfigPromotionStore) -> None:
    draft = promotion.create(
        draft_id="sponsor-draft",
        references=["system/core@1.0.0"],
        expected_active_digest=None,
        actor="sponsor",
    )

    validated = promotion.validate(
        draft.draft_id, actor="sponsor", sponsor_authored=True
    )

    assert validated.status == "approved"
    assert validated.decision["implicit"] is True
    assert validated.decision["decided_by"] == "sponsor"
    assert [event["action"] for event in validated.events] == ["validated", "approved"]
    assert promotion.validate(
        draft.draft_id, actor="sponsor", sponsor_authored=True
    ) == validated


def test_activation_resumes_after_pointer_moves_before_draft_checkpoint(
    promotion: ConfigPromotionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = promotion.create(
        draft_id="crash-resume",
        references=["system/core@1.0.0"],
        expected_active_digest=None,
        actor="sponsor",
    )
    approved = promotion.validate(
        draft.draft_id, actor="sponsor", sponsor_authored=True
    )
    original = promotion.activation._atomic_write
    failed = False

    def fail_checkpoint(path, payload):
        nonlocal failed
        if (
            path.name == "crash-resume.json"
            and payload.get("status") == "activated"
            and not failed
        ):
            failed = True
            raise ConfigActivationError("simulated checkpoint failure")
        original(path, payload)

    monkeypatch.setattr(promotion.activation, "_atomic_write", fail_checkpoint)
    with pytest.raises(ConfigActivationError, match="checkpoint"):
        promotion.activate(draft.draft_id, actor="sponsor", reason="promote")
    assert promotion.activation.get_state().active_digest == approved.validation["digest"]

    resumed = promotion.activate(draft.draft_id, actor="sponsor", reason="promote")

    assert resumed.status == "activated"
    assert promotion.activation.get_state().revision == 1


def test_invalid_stale_rejected_and_tampered_drafts_fail_closed(
    promotion: ConfigPromotionStore,
) -> None:
    invalid = promotion.create(
        draft_id="invalid",
        references=["system/missing@1.0.0"],
        expected_active_digest=None,
        actor="operator",
    )
    with pytest.raises(ConfigPromotionConflict, match="package not found"):
        promotion.validate(invalid.draft_id, actor="operator", sponsor_authored=False)
    assert promotion.get(invalid.draft_id).status == "draft"
    assert not promotion.activation.releases_dir.exists()

    rejected = promotion.create(
        draft_id="rejected",
        references=["system/core@1.0.0"],
        expected_active_digest=None,
        actor="operator",
    )
    promotion.validate(rejected.draft_id, actor="operator", sponsor_authored=False)
    promotion.decide(
        rejected.draft_id,
        sponsor_id="sponsor",
        decision="rejected",
        rationale="Not suitable",
    )
    with pytest.raises(ConfigPromotionConflict, match="not approved"):
        promotion.activate(rejected.draft_id, actor="operator", reason="forbidden")

    sponsor = promotion.create(
        draft_id="stale",
        references=["system/core@1.0.0"],
        expected_active_digest=None,
        actor="sponsor",
    )
    promotion.validate(sponsor.draft_id, actor="sponsor", sponsor_authored=True)
    settings = (
        promotion.activation.repository_root
        / "packages/system/core/1.0.0/settings.json"
    )
    settings.write_text('{"workers":99}\n', encoding="utf-8")
    with pytest.raises(ConfigPromotionConflict, match="content changed"):
        promotion.activate(sponsor.draft_id, actor="sponsor", reason="stale")


def test_structural_diff_is_stable_and_path_ordered() -> None:
    assert structural_diff(
        {"settings": {"b": 2, "a": 1}},
        {"settings": {"b": 3, "c": 4}},
    ) == [
        {"path": "$.settings.a", "change": "removed", "before": 1, "after": None},
        {"path": "$.settings.b", "change": "changed", "before": 2, "after": 3},
        {"path": "$.settings.c", "change": "added", "before": None, "after": 4},
    ]
