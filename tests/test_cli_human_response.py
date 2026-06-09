from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

from agentic_mesh import cli
from agentic_mesh.config import load_mesh_config
from agentic_mesh.controller_auth import ControllerAuthService
from agentic_mesh.human_gates import FileHumanGateRequestStore
from agentic_mesh.journal import EventJournal
from agentic_mesh.storage import FileMessageStore


class _FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_record_human_response_posts_to_live_control_plane_by_default(
    monkeypatch,
    capsys,
) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["form"] = parse_qs(request.data.decode("utf-8"))
        return _FakeResponse({"accepted": True, "message_id": "msg-live"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = cli.cmd_record_human_response(
        SimpleNamespace(
            work_item_id="work-approval",
            work_item_type="slice",
            lifecycle_state="release_review",
            gate_id="release_decision_response",
            approval_request_id=None,
            response_request_id="human-response-1",
            responder="sponsor",
            value="approve",
            role=None,
            source="cli",
            correlation_id=None,
            server_url="http://live-control-plane:8100",
            server_timeout=12,
            local_state=False,
        )
    )

    assert result == 0
    assert seen["url"] == "http://live-control-plane:8100/human-responses"
    assert seen["timeout"] == 12
    form = seen["form"]
    assert form["work_item_id"] == ["work-approval"]
    assert form["response_request_id"] == ["human-response-1"]
    assert form["value"] == ["approve"]
    assert json.loads(capsys.readouterr().out)["message_id"] == "msg-live"


def test_controller_human_response_endpoint_queues_validated_response(tmp_path: Path) -> None:
    mesh_config = load_mesh_config(Path.cwd())
    gate = next(
        gate
        for gate in mesh_config.project.flow.states["release_review"].gates
        if gate.gate_id == "release_decision_response"
    )
    store = FileHumanGateRequestStore(tmp_path, mesh_config.project.project_id)
    request, _ = store.ensure_request(
        work_item_id="work-release",
        work_item_type="slice",
        lifecycle_state="release_review",
        gate=gate,
    )
    store.mark_enqueue_succeeded(
        request.response_request_id,
        connector_message_id="conn-approval",
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/agentic-mesh-dev/agentic-mesh/project.yaml",
        state_root=tmp_path,
    )

    result = service.record_human_response(
        {
            "work_item_id": "work-release",
            "work_item_type": "slice",
            "lifecycle_state": "release_review",
            "gate_id": "release_decision_response",
            "response_request_id": request.response_request_id,
            "responder": "sponsor",
            "value": "approve",
            "source": "cli",
        }
    )

    assert result["accepted"] is True
    assert result["final"] is True
    assert result["message_id"].startswith("msg-")
    queued = FileMessageStore(
        tmp_path,
        mesh_config.project.project_id,
        EventJournal(tmp_path, mesh_config.project.project_id),
    ).claim_next("release-manager", "test-worker")
    assert queued is not None
    assert queued.type == "human_response.received"
    assert queued.payload["work_item_id"] == "work-release"
