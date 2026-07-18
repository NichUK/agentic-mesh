from __future__ import annotations

from pathlib import Path

from agentic_mesh_v5.worker_provider import EngineMetadata
from agentic_mesh_v5.worker_provider import ProviderEvent
from agentic_mesh_v5.worker_provider import ProviderEventKind
from agentic_mesh_v5.worker_provider import ThreadRequest
from agentic_mesh_v5.worker_provider import TurnCompletionStatus
from agentic_mesh_v5.worker_provider import TurnRequest
from agentic_mesh_v5.worker_provider import WorkerEngine
from agentic_mesh_v5.worker_provider import WorkerProvider
from agentic_mesh_v5.worker_provider import WorkerThread
from agentic_mesh_v5.worker_provider import WorkerTurn


ROOT = Path(__file__).resolve().parents[1]


class FakeTurn:
    thread_id = "thread-neutral"
    turn_id = "turn-neutral"

    def __init__(self) -> None:
        self.interrupted = False

    def events(self):
        yield ProviderEvent(
            ProviderEventKind.TURN_COMPLETED,
            self.thread_id,
            self.turn_id,
            completion=TurnCompletionStatus.COMPLETED,
        )

    def interrupt(self) -> None:
        self.interrupted = True


class FakeThread:
    thread_id = "thread-neutral"

    def __init__(self) -> None:
        self.turn = FakeTurn()

    def start_turn(self, _request: TurnRequest) -> FakeTurn:
        return self.turn


class FakeEngine:
    metadata = EngineMetadata("fake", "fake-runtime", "1", "test", "test")

    def __init__(self) -> None:
        self.thread = FakeThread()
        self.closed = False

    def start_thread(self, _request: ThreadRequest) -> FakeThread:
        return self.thread

    def resume_thread(self, _thread_id: str, _request: ThreadRequest) -> FakeThread:
        return self.thread

    def close(self) -> None:
        self.closed = True


class FakeProvider:
    provider_id = "fake"

    def __init__(self) -> None:
        self.engine = FakeEngine()

    def open(self) -> FakeEngine:
        return self.engine


def test_provider_protocol_is_independent_and_structural(tmp_path: Path) -> None:
    provider = FakeProvider()
    engine = provider.open()
    thread_request = ThreadRequest(
        cwd=tmp_path,
        base_instructions="private base prompt",
        developer_instructions="private developer prompt",
    )
    turn_request = TurnRequest("private work prompt")
    thread = engine.start_thread(thread_request)
    turn = thread.start_turn(turn_request)

    assert isinstance(provider, WorkerProvider)
    assert isinstance(engine, WorkerEngine)
    assert isinstance(thread, WorkerThread)
    assert isinstance(turn, WorkerTurn)
    assert [event.completion for event in turn.events()] == [
        TurnCompletionStatus.COMPLETED
    ]
    turn.interrupt()
    engine.close()
    assert turn.interrupted is True
    assert engine.closed is True
    assert "private" not in repr(thread_request)
    assert "private" not in repr(turn_request)


def test_neutral_provider_contract_has_no_codex_dependency() -> None:
    source = (
        ROOT / "src" / "agentic_mesh_v5" / "worker_provider.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "openai_codex" not in lowered
    assert "json-rpc" not in lowered
    assert "thread/start" not in lowered
    assert "turn/start" not in lowered
