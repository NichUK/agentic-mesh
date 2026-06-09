import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_mesh.capabilities import CapabilityReadinessStore
from agentic_mesh.capabilities import CapabilityValidator
from agentic_mesh.capabilities import build_merged_profile
from agentic_mesh.capabilities import capability_prompt_context
from agentic_mesh.capabilities import summarize_readiness
from agentic_mesh.capabilities import validate_and_store_all
from agentic_mesh.config import ConfigError
from agentic_mesh.config import load_mesh_config
from agentic_mesh.models import CapabilityConfig
from agentic_mesh.models import CapabilityProfileConfig
from agentic_mesh.models import CapabilityValidationConfig
from agentic_mesh.notifications import readiness_notification_event
from agentic_mesh.telemetry import TelemetryTestSink
from agentic_mesh.telemetry import record_capability_event
from agentic_mesh.telemetry import set_test_sink


def test_merged_profile_includes_defaults_project_role_and_default_tools() -> None:
    mesh = load_mesh_config(Path.cwd())
    instance = mesh.instances["agentic-mesh-dev.engineering.1"]

    profile = build_merged_profile(mesh, instance)
    keys = {(item.config.capability_id, item.config.category) for item in profile}

    assert ("worker.codex", "worker_capability") in keys
    assert ("role.implementation", "skill") in keys
    assert ("git.read", "compatibility_default_tool") in keys
    assert any(
        item.configured_sources == ("compatibility_default_tools",)
        for item in profile
        if item.config.capability_id == "git.read"
        and item.config.category == "compatibility_default_tool"
    )


def test_required_capability_downgrade_requires_fallback_or_waiver() -> None:
    mesh = load_mesh_config(Path.cwd())
    instance = mesh.instances["agentic-mesh-dev.engineering.1"]
    downgraded = CapabilityConfig(
        capability_id="worker.codex",
        category="worker_capability",
        requirement="optional",
        display_name="Codex worker adapter",
    )
    override = replace(
        instance.override,
        capabilities=CapabilityProfileConfig(capabilities=[downgraded]),
    )
    modified = replace(instance, override=override)
    modified_mesh = replace(
        mesh,
        instances={modified.instance_id: modified},
        project=replace(mesh.project, roles={modified.role_id: override}),
    )

    with pytest.raises(ValueError, match="Required capability cannot be downgraded"):
        build_merged_profile(modified_mesh, modified)


def test_validator_classifies_missing_required_optional_fallback_and_waiver(tmp_path: Path) -> None:
    mesh = load_mesh_config(Path.cwd())
    instance = mesh.instances["agentic-mesh-dev.engineering.1"]
    capabilities = CapabilityProfileConfig(
        capabilities=[
            CapabilityConfig(
                capability_id="missing.required",
                category="logical_tool",
                requirement="required",
                display_name="Missing required",
                validation=CapabilityValidationConfig(
                    kind="command",
                    command=["definitely-not-installed-agentic-mesh-tool"],
                ),
            ),
            CapabilityConfig(
                capability_id="missing.optional",
                category="logical_tool",
                requirement="optional",
                display_name="Missing optional",
                validation=CapabilityValidationConfig(
                    kind="command",
                    command=["definitely-not-installed-agentic-mesh-tool"],
                ),
            ),
            CapabilityConfig(
                capability_id="missing.fallback",
                category="logical_tool",
                requirement="required",
                display_name="Missing fallback",
                validation=CapabilityValidationConfig(
                    kind="command",
                    command=["definitely-not-installed-agentic-mesh-tool"],
                ),
                fallback={
                    "owner": "platform-engineer",
                    "policy_summary": "Use manual evidence.",
                    "residual_impact": "Manual relay required.",
                },  # type: ignore[arg-type]
            ),
        ]
    )
    # Use config parser for fallback dataclass validation in separate config tests;
    # here we patch the already-loaded role with real parsed defaults only.
    missing_required = CapabilityConfig(
        capability_id="missing.required",
        category="logical_tool",
        requirement="required",
        display_name="Missing required",
        validation=CapabilityValidationConfig(
            kind="command",
            command=["definitely-not-installed-agentic-mesh-tool"],
        ),
    )
    missing_optional = replace(missing_required, capability_id="missing.optional", requirement="optional")
    from agentic_mesh.models import CapabilityFallbackConfig
    from agentic_mesh.models import CapabilityWaiverConfig

    missing_fallback = replace(
        missing_required,
        capability_id="missing.fallback",
        fallback=CapabilityFallbackConfig(
            owner="platform-engineer",
            policy_summary="Use manual evidence.",
            residual_impact="Manual relay required.",
        ),
    )
    missing_waived = replace(
        missing_required,
        capability_id="missing.waived",
        waiver=CapabilityWaiverConfig(
            owner="security-architect",
            reason="Local dogfood exception.",
            residual_impact="Not enterprise-ready.",
            review_point="Before release.",
        ),
    )
    override = replace(
        instance.override,
        capabilities=CapabilityProfileConfig(
            capabilities=[
                missing_required,
                missing_optional,
                missing_fallback,
                missing_waived,
            ]
        ),
    )
    patched = replace(instance, override=override)
    patched_mesh = replace(
        mesh,
        instances={patched.instance_id: patched},
        project=replace(mesh.project, roles={patched.role_id: override}),
    )

    results = CapabilityValidator(
        mesh_config=patched_mesh,
        workspace_root=tmp_path,
        now="2026-06-05T00:00:00+00:00",
    ).validate_instance(patched)
    by_id = {result.capability_id: result for result in results}

    assert by_id["missing.required"].status == "missing_required"
    assert by_id["missing.optional"].status == "missing_optional"
    assert by_id["missing.fallback"].status == "unavailable_with_fallback"
    assert by_id["missing.waived"].status == "waived"


def test_readiness_store_writes_atomic_safe_current_and_snapshot(tmp_path: Path) -> None:
    mesh = load_mesh_config(Path.cwd())
    instance = mesh.instances["agentic-mesh-dev.engineering.1"]
    results = CapabilityValidator(
        mesh_config=mesh,
        workspace_root=Path.cwd(),
        now="2026-06-05T00:00:00+00:00",
    ).validate_instance(instance)
    summary = summarize_readiness(
        project_id=instance.project_id,
        role_id=instance.role_id,
        role_instance_id=instance.instance_id,
        results=results,
        generated_at="2026-06-05T00:00:00+00:00",
    )
    store = CapabilityReadinessStore(tmp_path, mesh.project.project_id)

    current = store.write_current(instance.instance_id, summary=summary, results=results)
    current_path = (
        tmp_path
        / "projects"
        / mesh.project.project_id
        / "capability-readiness"
        / instance.instance_id
        / "current.json"
    )

    assert current["summary"]["redaction_applied"] is True
    assert oct(current_path.stat().st_mode & 0o777) == "0o600"
    snapshot_id, snapshot = store.snapshot_current(instance.instance_id, snapshot_id="snap-safe")
    assert snapshot_id == "snap-safe"
    assert snapshot["current"]["summary"]["role_instance_id"] == instance.instance_id


def test_corrupt_readiness_evidence_returns_unknown(tmp_path: Path) -> None:
    mesh = load_mesh_config(Path.cwd())
    instance = mesh.instances["agentic-mesh-dev.engineering.1"]
    store = CapabilityReadinessStore(tmp_path, mesh.project.project_id)
    root = (
        tmp_path
        / "projects"
        / mesh.project.project_id
        / "capability-readiness"
        / instance.instance_id
    )
    root.mkdir(parents=True)
    (root / "current.json").write_text("{not-json", encoding="utf-8")

    current = store.read_current(instance.instance_id)

    assert current is not None
    assert current["summary"]["overall_readiness"] == "unknown"
    assert current["summary"]["diagnostic"]["error_class"] == "unreadable_evidence"


def test_validate_and_store_all_covers_every_dogfood_instance(tmp_path: Path) -> None:
    mesh = load_mesh_config(Path.cwd())

    report = validate_and_store_all(
        mesh_config=mesh,
        workspace_root=Path.cwd(),
        state_root=tmp_path,
        generated_at="2026-06-05T00:00:00+00:00",
    )

    assert {row["role_instance_id"] for row in report["roles"]} == set(mesh.instances)
    assert all(row["summary"]["redaction_applied"] for row in report["roles"])
    assert "secret_ref" not in json.dumps(report)
    assert "mount_ref" not in json.dumps(report)
    assert "stdout" not in json.dumps(report)


def test_prompt_context_separates_configured_from_validated(tmp_path: Path) -> None:
    mesh = load_mesh_config(Path.cwd())
    instance = mesh.instances["agentic-mesh-dev.engineering.1"]

    no_evidence = capability_prompt_context(
        mesh_config=mesh,
        instance=instance,
        state_root=tmp_path,
    )
    assert "not been validated" in no_evidence["availability_statement"]
    assert no_evidence["available"] == []

    validate_and_store_all(
        mesh_config=mesh,
        workspace_root=Path.cwd(),
        state_root=tmp_path,
        generated_at="2026-06-05T00:00:00+00:00",
    )
    with_evidence = capability_prompt_context(
        mesh_config=mesh,
        instance=instance,
        state_root=tmp_path,
    )
    assert with_evidence["configured_required"]
    assert with_evidence["available"]
    assert "command" not in json.dumps(with_evidence)


def test_readiness_notification_skips_optional_missing_and_notifies_required() -> None:
    required = {
        "project_id": "agentic-mesh-dev",
        "role_id": "engineering",
        "role_instance_id": "agentic-mesh-dev.engineering.1",
        "capability_id": "tests.run",
        "display_name": "Tests run",
        "status": "missing_required",
        "user_label": "Missing Required",
        "impact": "Capability-dependent work is blocked.",
        "next_action": "Install test runner.",
        "action_owner": "platform-engineer",
    }
    optional = dict(required, status="missing_optional", user_label="Missing Optional")
    summary = {
        "project_id": "agentic-mesh-dev",
        "role_id": "engineering",
        "overall_readiness": "not_ready_for_capability_dependent_work",
        "status_url": "/agents/current.json",
    }

    assert readiness_notification_event(readiness_result=optional, summary=summary) is None
    event = readiness_notification_event(readiness_result=required, summary=summary)

    assert event is not None
    payload = event.to_dict()
    assert payload["event_kind"] == "capability.readiness.action_needed"
    assert payload["action_needed"] is True
    assert "secret_ref" not in json.dumps(payload)


def test_capability_telemetry_uses_allowlisted_attributes() -> None:
    sink = TelemetryTestSink()
    set_test_sink(sink)
    try:
        record_capability_event(
            "capability.validation.completed",
            project_id="agentic-mesh-dev",
            role_id="engineering",
            role_instance_id="agentic-mesh-dev.engineering.1",
            capability_id="tests.run",
            category="logical_tool",
            status="available",
            previous_status="missing_required",
            presentation_group="available",
            action_owner="engineering",
            snapshot_id="snap-safe",
            correlation_id="corr-safe",
            evidence_freshness="fresh",
            redacted_error_class="none",
        )
    finally:
        set_test_sink(None)

    assert sink.logs[0]["event_type"] == "capability.validation.completed"
    attrs = sink.metrics[0]["attributes"]
    assert attrs["capability_id"] == "tests.run"
    assert "command" not in attrs
    assert "mount_ref" not in attrs
