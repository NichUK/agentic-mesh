from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from threading import Barrier
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from agentic_mesh_v5.database import MigrationRunner
from agentic_mesh_v5.import_discovery import BindingSource
from agentic_mesh_v5.import_discovery import DiscoverySourceUnavailable
from agentic_mesh_v5.import_discovery import ImportDiscoveryRequest
from agentic_mesh_v5.import_discovery import ProjectImportDiscovery
from agentic_mesh_v5.import_discovery import RepositoryInventory
from agentic_mesh_v5.import_discovery import RepositorySource
from agentic_mesh_v5.import_questions import AnswerDraft
from agentic_mesh_v5.import_questions import ImportQuestion
from agentic_mesh_v5.import_questions import ImportQuestionConflict
from agentic_mesh_v5.import_questions import ImportQuestionError
from agentic_mesh_v5.import_questions import ImportQuestionNotReady
from agentic_mesh_v5.import_questions import ImportQuestionStore
from agentic_mesh_v5.import_questions import ImportQuestionStoreError


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


@pytest.fixture
def question_store(postgres_database: str) -> ImportQuestionStore:
    assert MigrationRunner(postgres_database).migrate().current_version == 30
    return ImportQuestionStore(postgres_database)


class _RepositoryDiscovery:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls = 0

    def inspect(self, source: RepositorySource) -> RepositoryInventory:
        self.calls += 1
        if self.unavailable:
            raise DiscoverySourceUnavailable("private provider detail")
        return RepositoryInventory("main", "a" * 40, 2, "b" * 64)


def _report(
    *,
    project_hints: bool = False,
    unavailable: bool = False,
    repository_id: str = "runtime",
):
    repository = _RepositoryDiscovery(unavailable=unavailable)
    report = ProjectImportDiscovery(
        repositories=repository,
        documents={},
    ).discover(
        ImportDiscoveryRequest(
            "sample-project" if project_hints else None,
            "Sample Project" if project_hints else None,
            repositories=(
                RepositorySource(
                    repository_id,
                    f"https://github.com/example/{repository_id}.git",
                    "main",
                    "oauth-cache://github/current",
                ),
            ),
            bindings=(
                BindingSource(
                    "ado",
                    "ado",
                    "https://dev.azure.com/example/sample-project",
                    "oauth-cache://ado/current",
                ),
            ),
        )
    )
    return report, repository


def _answer_for(question: ImportQuestion) -> AnswerDraft:
    if question.answer_kind == "identifier":
        return AnswerDraft("sample-project")
    if question.answer_kind == "string-list":
        return AnswerDraft(["primary"])
    if question.answer_kind == "credential-policy":
        return AnswerDraft("none-public")
    return AnswerDraft(f"Answer for {question.question_id}")


def _start(store: ImportQuestionStore, report=None):
    selected = _report()[0] if report is None else report
    import_id = str(uuid.uuid4())
    state = store.start(import_id=import_id, report=selected, actor_id="pm@example.com")
    return import_id, state


def test_start_pins_discovery_and_builds_complete_initial_question_set(
    question_store: ImportQuestionStore,
) -> None:
    report, repository = _report(unavailable=True)
    import_id, state = _start(question_store, report)
    question_ids = {item.question_id for item in state.questions}

    assert repository.calls == 1
    assert state.import_id == import_id
    assert state.discovery_digest == report.digest
    assert state.status == "questioning"
    assert state.version == 1
    assert state.answers == {}
    assert {
        "project.id",
        "project.display-name",
        "project.intent",
        "project.owner",
        "project.sponsors",
        "source.repository.runtime.purpose",
        "source.repository.runtime.owner",
        "source.repository.runtime.credential-policy",
        "source.repository.runtime.default-branch",
        "source.repository.runtime.disposition.source-unavailable",
        "source.binding.ado.purpose",
        "deployment.strategy",
        "deployment.environments",
        "deployment.owner",
    } <= question_ids
    assert len(state.questionnaire_digest) == 64
    assert question_store.history(import_id) == ()


def test_multiple_answer_rounds_survive_restart_and_unlock_pinned_preview(
    postgres_database: str,
) -> None:
    assert MigrationRunner(postgres_database).migrate().current_version == 30
    first_store = ImportQuestionStore(postgres_database)
    import_id, state = _start(first_store, _report(project_hints=True)[0])
    split = len(state.questions) // 2
    first_answers = {
        item.question_id: _answer_for(item) for item in state.questions[:split]
    }

    partial = first_store.answer(
        import_id=import_id,
        answers=first_answers,
        actor_id="pm@example.com",
        expected_version=state.version,
        rationale="First sponsor answer round",
    )
    restarted = ImportQuestionStore(postgres_database)
    observed = restarted.get(import_id)
    remaining = {
        item.question_id: _answer_for(item)
        for item in observed.questions
        if item.question_id not in observed.answers
    }
    ready = restarted.answer(
        import_id=import_id,
        answers=remaining,
        actor_id="pm@example.com",
        expected_version=observed.version,
        rationale="Complete the remaining facts",
    )
    resolution = restarted.require_preview_ready(
        import_id=import_id, expected_version=ready.version
    )

    assert partial.status == "questioning"
    assert observed == partial
    assert ready.status == "ready"
    assert ready.unanswered_question_ids == ()
    assert resolution.discovery_digest == ready.discovery_digest
    assert resolution.discovery_report == ready.discovery_report
    assert resolution.questionnaire_digest == ready.questionnaire_digest
    assert resolution.session_version == ready.version
    assert resolution.answers == ready.answers
    assert len(resolution.resolution_digest) == 64
    assert [item["event_kind"] for item in restarted.history(import_id)] == [
        "answers-recorded",
        "answers-recorded",
    ]


def test_preview_is_blocked_for_partial_and_stale_sessions(
    question_store: ImportQuestionStore,
) -> None:
    import_id, state = _start(question_store)
    partial = question_store.answer(
        import_id=import_id,
        answers={state.questions[0].question_id: _answer_for(state.questions[0])},
        actor_id="pm@example.com",
        expected_version=state.version,
    )

    with pytest.raises(ImportQuestionNotReady, match="every material question"):
        question_store.require_preview_ready(
            import_id=import_id, expected_version=partial.version
        )
    with pytest.raises(ImportQuestionConflict, match="version changed"):
        question_store.require_preview_ready(
            import_id=import_id, expected_version=state.version
        )


def test_contradictory_round_is_atomic_and_explicit_correction_is_audited(
    question_store: ImportQuestionStore,
) -> None:
    import_id, state = _start(question_store)
    question_id = "project.intent"
    first = question_store.answer(
        import_id=import_id,
        answers={question_id: AnswerDraft("Initial intent")},
        actor_id="pm@example.com",
        expected_version=state.version,
    )

    with pytest.raises(ImportQuestionConflict, match="different answer"):
        question_store.answer(
            import_id=import_id,
            answers={question_id: AnswerDraft("Contradictory intent")},
            actor_id="pm@example.com",
            expected_version=first.version,
        )
    unchanged = question_store.get(import_id)
    corrected = question_store.answer(
        import_id=import_id,
        answers={
            question_id: AnswerDraft(
                "Corrected intent",
                replace=True,
                correction_reason="Sponsor corrected the original scope",
            )
        },
        actor_id="sponsor@example.com",
        expected_version=unchanged.version,
    )
    history = question_store.history(import_id)

    assert unchanged == first
    assert corrected.answers[question_id] == "Corrected intent"
    assert corrected.version == first.version + 1
    assert len(history) == 2
    correction = history[1]["payload"]["answers"][0]
    assert correction["replaced_value"] == "Initial intent"
    assert correction["value"] == "Corrected intent"
    assert correction["correction_reason"] == "Sponsor corrected the original scope"


def test_follow_up_question_relocks_ready_session_until_answered(
    question_store: ImportQuestionStore,
) -> None:
    import_id, state = _start(question_store, _report(project_hints=True)[0])
    ready = question_store.answer(
        import_id=import_id,
        answers={item.question_id: _answer_for(item) for item in state.questions},
        actor_id="pm@example.com",
        expected_version=state.version,
    )
    followup = ImportQuestion(
        "architecture.boundary",
        "project",
        "Which architecture boundary owns the shared API?",
        "text",
    )

    reopened = question_store.add_followups(
        import_id=import_id,
        questions=(followup,),
        actor_id="pm@example.com",
        rationale="The deployment answer exposed a shared API",
        expected_version=ready.version,
    )
    with pytest.raises(ImportQuestionNotReady):
        question_store.require_preview_ready(
            import_id=import_id, expected_version=reopened.version
        )
    completed = question_store.answer(
        import_id=import_id,
        answers={followup.question_id: AnswerDraft("Platform team boundary")},
        actor_id="sponsor@example.com",
        expected_version=reopened.version,
    )

    assert reopened.status == "questioning"
    assert reopened.unanswered_question_ids == (followup.question_id,)
    assert completed.status == "ready"
    assert [item["event_kind"] for item in question_store.history(import_id)] == [
        "answers-recorded",
        "questions-added",
        "answers-recorded",
    ]


def test_duplicate_start_and_identical_updates_are_idempotent(
    question_store: ImportQuestionStore,
) -> None:
    report = _report(project_hints=True)[0]
    import_id, first = _start(question_store, report)
    repeated = question_store.start(
        import_id=import_id, report=report, actor_id="another-pm@example.com"
    )
    question = first.questions[0]
    answered = question_store.answer(
        import_id=import_id,
        answers={question.question_id: _answer_for(question)},
        actor_id="pm@example.com",
        expected_version=first.version,
    )
    answer_replay = question_store.answer(
        import_id=import_id,
        answers={question.question_id: _answer_for(question)},
        actor_id="pm@example.com",
        expected_version=first.version,
    )
    followup = ImportQuestion("project.constraint", "project", "Constraint?", "text")
    added = question_store.add_followups(
        import_id=import_id,
        questions=(followup,),
        actor_id="pm@example.com",
        rationale="Material constraint",
        expected_version=answered.version,
    )
    followup_replay = question_store.add_followups(
        import_id=import_id,
        questions=(followup,),
        actor_id="pm@example.com",
        rationale="Material constraint",
        expected_version=answered.version,
    )

    assert repeated == first
    assert answer_replay == answered
    assert followup_replay == added
    assert len(question_store.history(import_id)) == 2


def test_concurrent_start_is_idempotent(question_store: ImportQuestionStore) -> None:
    report = _report(project_hints=True)[0]
    import_id = str(uuid.uuid4())
    barrier = Barrier(2)

    def start(actor: str):
        barrier.wait()
        return question_store.start(import_id=import_id, report=report, actor_id=actor)

    with ThreadPoolExecutor(max_workers=2) as executor:
        states = tuple(executor.map(start, ("pm-one", "pm-two")))

    assert states[0] == states[1]
    assert states[0].version == 1
    assert question_store.history(import_id) == ()


def test_same_import_id_rejects_another_discovery_report(
    question_store: ImportQuestionStore,
) -> None:
    import_id, state = _start(question_store, _report(repository_id="runtime")[0])

    with pytest.raises(ImportQuestionConflict, match="another discovery"):
        question_store.start(
            import_id=import_id,
            report=_report(repository_id="portal")[0],
            actor_id="pm@example.com",
        )
    assert question_store.get(import_id) == state


def test_conflicting_source_declarations_receive_distinct_questions(
    question_store: ImportQuestionStore,
) -> None:
    report = ProjectImportDiscovery(
        repositories=_RepositoryDiscovery(),
        documents={},
    ).discover(
        ImportDiscoveryRequest(
            None,
            None,
            repositories=(
                RepositorySource("repo", "https://github.com/example/one.git"),
                RepositorySource("repo", "https://github.com/example/two.git"),
            ),
        )
    )

    _, state = _start(question_store, report)
    ids = {item.question_id for item in state.questions}

    assert "source.repository.repo.declaration-1.purpose" in ids
    assert "source.repository.repo.declaration-2.purpose" in ids
    assert "source.repository.repo.disposition.source-id-conflict" in ids


def test_tampered_discovery_report_is_rejected_before_persistence(
    question_store: ImportQuestionStore,
) -> None:
    report = replace(_report()[0], digest="0" * 64)

    with pytest.raises(ImportQuestionConflict, match="digest"):
        question_store.start(
            import_id=str(uuid.uuid4()), report=report, actor_id="pm@example.com"
        )


def test_unknown_question_and_embedded_credential_fail_atomically(
    question_store: ImportQuestionStore,
) -> None:
    import_id, state = _start(question_store)
    credential = next(
        item for item in state.questions if item.answer_kind == "credential-policy"
    )

    with pytest.raises(ImportQuestionConflict, match="not part"):
        question_store.answer(
            import_id=import_id,
            answers={"unknown.question": AnswerDraft("answer")},
            actor_id="pm@example.com",
            expected_version=state.version,
        )
    with pytest.raises(ImportQuestionError, match="external reference"):
        question_store.answer(
            import_id=import_id,
            answers={
                "project.intent": AnswerDraft("Valid intent"),
                credential.question_id: AnswerDraft("real-token-value"),
            },
            actor_id="pm@example.com",
            expected_version=state.version,
        )
    with pytest.raises(ImportQuestionError, match="secret value"):
        question_store.answer(
            import_id=import_id,
            answers={"project.intent": AnswerDraft("token=raw-provider-value")},
            actor_id="pm@example.com",
            expected_version=state.version,
        )

    assert question_store.get(import_id) == state
    assert question_store.history(import_id) == ()


def test_concurrent_rounds_allow_only_one_writer_at_a_version(
    question_store: ImportQuestionStore,
) -> None:
    import_id, state = _start(question_store)
    barrier = Barrier(2)
    questions = state.questions[:2]

    def submit(question: ImportQuestion):
        barrier.wait()
        try:
            return question_store.answer(
                import_id=import_id,
                answers={question.question_id: _answer_for(question)},
                actor_id="pm@example.com",
                expected_version=state.version,
            )
        except ImportQuestionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(submit, questions))

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, ImportQuestionConflict) for item in outcomes) == 1
    observed = question_store.get(import_id)
    assert observed.version == 2
    assert len(observed.answers) == 1
    assert len(question_store.history(import_id)) == 1


def test_persisted_digest_corruption_fails_closed(
    question_store: ImportQuestionStore, postgres_database: str
) -> None:
    import_id, _ = _start(question_store)
    with psycopg.connect(postgres_database, autocommit=True) as connection:
        connection.execute(
            """
            UPDATE agentic_mesh_v5.project_import_question_sessions
            SET questionnaire_digest = %s WHERE import_id = %s
            """,
            ("f" * 64, import_id),
        )

    with pytest.raises(ImportQuestionStoreError, match="read failed"):
        question_store.get(import_id)
