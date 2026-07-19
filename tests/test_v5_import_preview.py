from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
from threading import Barrier, Lock
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.import_discovery import BindingSource
from agentic_mesh_v5.import_discovery import ImportDiscoveryRequest
from agentic_mesh_v5.import_discovery import ProjectImportDiscovery
from agentic_mesh_v5.import_discovery import RepositoryInventory
from agentic_mesh_v5.import_discovery import RepositorySource
from agentic_mesh_v5.import_preview import BacklogCandidate
from agentic_mesh_v5.import_preview import ImportActivationError
from agentic_mesh_v5.import_preview import ImportActivationReceipt
from agentic_mesh_v5.import_preview import ImportPreviewConflict
from agentic_mesh_v5.import_preview import ImportPreviewStore
from agentic_mesh_v5.import_preview import SourceSelection
from agentic_mesh_v5.import_preview import preview_source_keys
from agentic_mesh_v5.import_questions import AnswerDraft
from agentic_mesh_v5.import_questions import ImportQuestion
from agentic_mesh_v5.import_questions import ImportQuestionStore
from agentic_mesh_v5.project_manifest import load_project_manifest


ROOT = Path(__file__).parents[1]
MANIFEST_PATH = (
    ROOT / "examples/projects/agentic-mesh-v5/agentic-mesh/project.yaml"
)


@pytest.fixture
def postgres_database() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for Postgres tests")
    database_name = f"mesh_v5_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    database_url = urlunsplit(urlsplit(base_url)._replace(path=f"/{database_name}"))
    try:
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


class _Repositories:
    def inspect(self, source: RepositorySource) -> RepositoryInventory:
        return RepositoryInventory("develop", "a" * 40, 3, "b" * 64)


def _answer(question: ImportQuestion) -> AnswerDraft:
    if question.question_id == "project.sponsors":
        return AnswerDraft(["sponsor@example.com"])
    if question.answer_kind == "string-list":
        return AnswerDraft(["production"])
    if question.answer_kind == "credential-policy":
        return AnswerDraft("oauth-cache://import/current")
    if question.answer_kind == "identifier":
        return AnswerDraft("agentic-mesh-v5")
    return AnswerDraft(f"Resolved {question.question_id}")


@pytest.fixture
def ready_import(postgres_database: str):
    assert MigrationRunner(postgres_database).migrate().current_version == 31
    report = ProjectImportDiscovery(
        repositories=_Repositories(),
        documents={},
    ).discover(
        ImportDiscoveryRequest(
            "agentic-mesh-v5",
            "Agentic Mesh V5",
            repositories=(
                RepositorySource(
                    "runtime",
                    "https://github.com/NichUK/agentic-mesh.git",
                    "develop",
                    "oauth-cache://github/current",
                ),
            ),
            bindings=(
                BindingSource(
                    "ado",
                    "ado",
                    "https://dev.azure.com/seerstone/agentic-mesh",
                    "oauth-cache://ado/current",
                ),
            ),
        )
    )
    import_id = str(uuid.uuid4())
    questions = ImportQuestionStore(postgres_database)
    state = questions.start(
        import_id=import_id, report=report, actor_id="project-manager"
    )
    ready = questions.answer(
        import_id=import_id,
        answers={item.question_id: _answer(item) for item in state.questions},
        actor_id="project-manager",
        expected_version=state.version,
    )
    resolution = questions.require_preview_ready(
        import_id=import_id, expected_version=ready.version
    )
    return {
        "database_url": postgres_database,
        "import_id": import_id,
        "questions": questions,
        "resolution": resolution,
        "manifest": load_project_manifest(MANIFEST_PATH),
        "store": ImportPreviewStore(postgres_database),
    }


def _selections(resolution) -> tuple[SourceSelection, ...]:
    result = []
    for source_key in preview_source_keys(resolution):
        if source_key.startswith("repository/"):
            result.append(
                SourceSelection(
                    source_key,
                    "include",
                    "Primary source repository",
                    "repository",
                    "https://github.com/NichUK/agentic-mesh.git",
                )
            )
        else:
            result.append(
                SourceSelection(
                    source_key,
                    "include",
                    "Configured ADO project",
                    "ado-project",
                    "https://dev.azure.com/seerstone|agentic-mesh",
                )
            )
    return tuple(result)


def _candidates(resolution) -> tuple[BacklogCandidate, ...]:
    keys = preview_source_keys(resolution)
    return (
        BacklogCandidate(
            "candidate-1",
            "Finish documented API work",
            "Unfinished work identified from repository evidence",
            (keys[0],),
        ),
        BacklogCandidate(
            "candidate-2",
            "Review deferred integration",
            "Potential work requiring sponsor selection",
            (keys[1],),
        ),
    )


def _preview(context, *, expected=0, selected=("candidate-1",), candidates=None):
    resolution = context["resolution"]
    return context["store"].create_preview(
        resolution=resolution,
        manifest=context["manifest"],
        source_selections=_selections(resolution),
        backlog_candidates=_candidates(resolution) if candidates is None else candidates,
        selected_candidate_ids=selected,
        expected_prior_revision=expected,
        actor_id="project-manager",
    )


def test_preview_is_validated_source_complete_and_keeps_candidates_inactive(
    ready_import,
) -> None:
    preview = _preview(ready_import)
    assert preview.revision == 1
    assert preview.status == "draft"
    assert preview.project_id == "agentic-mesh-v5"
    assert preview.manifest_digest == ready_import["manifest"].digest
    assert all(item.decision == "include" for item in preview.source_selections)
    assert preview.selected_candidate_ids == ("candidate-1",)
    assert [item.to_dict()["status"] for item in preview.backlog_candidates] == ["candidate"] * 2
    with psycopg.connect(ready_import["database_url"]) as connection:
        assert connection.execute(
            "SELECT (SELECT count(*) FROM agentic_mesh_v5.projects), "
            "(SELECT count(*) FROM agentic_mesh_v5.work_items)"
        ).fetchone() == (0, 0)


def test_preview_revision_and_exact_replay_preserve_immutable_history(
    ready_import,
) -> None:
    first = _preview(ready_import)
    replay = _preview(ready_import)
    second = _preview(
        ready_import,
        expected=1,
        selected=("candidate-2",),
    )
    assert replay == first
    assert second.selected_candidate_ids == ("candidate-2",)
    assert ready_import["store"].get_preview(first.import_id, 1).status == "superseded"
    assert ready_import["store"].get_preview(first.import_id, 2).status == "draft"
    with pytest.raises(ImportPreviewConflict, match="revision changed"):
        _preview(ready_import, expected=0, selected=())


@pytest.mark.parametrize(
    "case", ["missing-source", "resource", "candidate", "selected", "excluded"]
)
def test_preview_validation_fails_closed(ready_import, case: str) -> None:
    resolution = ready_import["resolution"]
    selections = list(_selections(resolution))
    candidates = list(_candidates(resolution))
    selected = ["candidate-1"]
    if case == "missing-source":
        selections.pop()
    elif case == "resource":
        selections[0] = replace(selections[0], resource_key="missing-resource")
    elif case == "candidate":
        candidates[0] = BacklogCandidate(
            "candidate-1", "Unknown", "Unknown source", ("unknown/source/key/1",)
        )
    elif case == "selected":
        selected = ["missing-candidate"]
    else:
        key = candidates[0].source_keys[0]
        index = next(i for i, item in enumerate(selections) if item.source_key == key)
        selections[index] = SourceSelection(key, "exclude", "Excluded by sponsor")
    with pytest.raises(ImportPreviewConflict):
        ready_import["store"].create_preview(
            resolution=resolution,
            manifest=ready_import["manifest"],
            source_selections=selections,
            backlog_candidates=candidates,
            selected_candidate_ids=selected,
            expected_prior_revision=0,
            actor_id="project-manager",
        )
    with psycopg.connect(ready_import["database_url"]) as connection:
        assert connection.execute(
            "SELECT count(*) FROM agentic_mesh_v5.project_import_previews"
        ).fetchone()[0] == 0


def test_only_configured_sponsor_can_decide_latest_revision(ready_import) -> None:
    preview = _preview(ready_import)
    with pytest.raises(ImportPreviewConflict, match="not a project sponsor"):
        ready_import["store"].decide(
            import_id=preview.import_id,
            revision=preview.revision,
            sponsor_id="intruder@example.com",
            decision="approved",
            rationale="Unauthorized",
        )
    decision = ready_import["store"].decide(
        import_id=preview.import_id,
        revision=preview.revision,
        sponsor_id="sponsor@example.com",
        decision="approved",
        rationale="Approved exact preview",
        evidence={"review": "sponsor-review-1"},
    )
    replay = ready_import["store"].decide(
        import_id=preview.import_id,
        revision=preview.revision,
        sponsor_id="sponsor@example.com",
        decision="approved",
        rationale="Approved exact preview",
        evidence={"review": "sponsor-review-1"},
    )
    assert decision == replay
    assert ready_import["store"].get_preview(preview.import_id, 1).status == "approved"
    with pytest.raises(ImportPreviewConflict, match="cannot be revised"):
        _preview(ready_import, expected=1, selected=())


def test_rejected_preview_can_be_revised_but_approval_does_not_carry_forward(
    ready_import,
) -> None:
    first = _preview(ready_import)
    ready_import["store"].decide(
        import_id=first.import_id,
        revision=1,
        sponsor_id="sponsor@example.com",
        decision="rejected",
        rationale="Choose a different candidate",
    )
    second = _preview(ready_import, expected=1, selected=("candidate-2",))
    assert ready_import["store"].get_preview(first.import_id, 1).status == "superseded"
    with pytest.raises(ImportPreviewConflict, match="not sponsor approved"):
        _activate(ready_import, second, _Activator())


class _Activator:
    def __init__(self, *, fail_once: bool = False, mismatch: bool = False) -> None:
        self.fail_once = fail_once
        self.mismatch = mismatch
        self.calls = []
        self._lock = Lock()

    def activate(self, request):
        with self._lock:
            self.calls.append(request)
            should_fail = self.fail_once
            self.fail_once = False
        if should_fail:
            raise RuntimeError("private activation failure")
        return ImportActivationReceipt(
            "wrong-project" if self.mismatch else request.manifest.project_id,
            request.manifest.digest,
            tuple(item.candidate_id for item in request.selected_candidates),
            f"activation:{request.operation_id}",
        )


def _approved(context):
    preview = _preview(context)
    context["store"].decide(
        import_id=preview.import_id,
        revision=1,
        sponsor_id="sponsor@example.com",
        decision="approved",
        rationale="Approve import",
    )
    return preview


def _activate(context, preview, activator, operation_id="activate-1"):
    return context["store"].activate(
        import_id=preview.import_id,
        revision=preview.revision,
        operation_id=operation_id,
        actor_id="project-manager",
        activator=activator,
    )


def test_activation_is_exactly_replayable_and_selects_only_approved_candidates(
    ready_import,
) -> None:
    preview = _approved(ready_import)
    activator = _Activator()
    first = _activate(ready_import, preview, activator)
    replay = _activate(ready_import, preview, activator)
    assert first == replay
    assert len(activator.calls) == 1
    assert [item.candidate_id for item in activator.calls[0].selected_candidates] == [
        "candidate-1"
    ]
    assert ready_import["store"].get_preview(preview.import_id, 1).status == "activated"
    with pytest.raises(ImportPreviewConflict, match="another activation"):
        _activate(ready_import, preview, activator, "activate-2")


def test_import_session_deletion_cascades_through_completed_activation(
    ready_import,
) -> None:
    preview = _approved(ready_import)
    _activate(ready_import, preview, _Activator())
    with psycopg.connect(ready_import["database_url"]) as connection:
        connection.execute(
            "DELETE FROM agentic_mesh_v5.project_import_question_sessions WHERE import_id = %s",
            (preview.import_id,),
        )
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM agentic_mesh_v5.project_import_previews), "
            "(SELECT count(*) FROM agentic_mesh_v5.project_import_preview_decisions), "
            "(SELECT count(*) FROM agentic_mesh_v5.project_import_activations)"
        ).fetchone()
    assert counts == (0, 0, 0)


def test_failed_activation_resumes_same_operation_without_terminal_state(
    ready_import,
) -> None:
    preview = _approved(ready_import)
    activator = _Activator(fail_once=True)
    with pytest.raises(ImportActivationError, match="adapter failed"):
        _activate(ready_import, preview, activator)
    receipt = _activate(ready_import, preview, activator)
    assert len(activator.calls) == 2
    with psycopg.connect(ready_import["database_url"]) as connection:
        assert connection.execute(
            """
            SELECT status, attempts, last_error
            FROM agentic_mesh_v5.project_import_activations
            WHERE import_id = %s
            """,
            (preview.import_id,),
        ).fetchone() == ("activated", 2, None)


def test_concurrent_duplicate_activation_calls_adapter_once(ready_import) -> None:
    preview = _approved(ready_import)
    activator = _Activator()
    barrier = Barrier(2)

    def run():
        barrier.wait()
        return _activate(ready_import, preview, activator)
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(lambda _: run(), range(2)))
    assert receipts[0] == receipts[1]
    assert len(activator.calls) == 1


def test_mismatched_receipt_does_not_claim_activation(ready_import) -> None:
    preview = _approved(ready_import)
    with pytest.raises(ImportActivationError, match="receipt disagrees"):
        _activate(ready_import, preview, _Activator(mismatch=True))
    assert ready_import["store"].get_preview(preview.import_id, 1).status == "approved"


def test_preview_rejects_resolution_after_new_material_question(ready_import) -> None:
    preview = _preview(ready_import)
    state = ready_import["questions"].get(ready_import["import_id"])
    ready_import["questions"].add_followups(
        import_id=ready_import["import_id"],
        questions=(
            ImportQuestion(
                "import.additional-boundary",
                "project",
                "Which additional boundary was discovered?",
                "text",
            ),
        ),
        actor_id="project-manager",
        rationale="New material ambiguity",
        expected_version=state.version,
    )
    with pytest.raises(ImportPreviewConflict, match="no longer current"):
        ready_import["store"].decide(
            import_id=preview.import_id,
            revision=preview.revision,
            sponsor_id="sponsor@example.com",
            decision="approved",
            rationale="Stale approval",
        )
