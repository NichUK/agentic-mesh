from __future__ import annotations

import json
import time
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

from agentic_mesh import controller_auth
from agentic_mesh.controller_auth import ControllerAuthHandler
from agentic_mesh.controller_auth import ControllerAuthServer
from agentic_mesh.controller_auth import ControllerAuthService


def test_controller_auth_status_json_lists_reusable_credentials(tmp_path: Path) -> None:
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/auth/status.json")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert body["credentials"][0]["credential"] == "codex-example-shared-api-key"
        assert body["credentials"][0]["status"] == "missing"
        assert body["credentials"][0]["redacted"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_form_stores_secret_without_echoing_value(
    tmp_path: Path,
) -> None:
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file="examples/projects/example-project/agentic-mesh/project.yaml",
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)
        body = (
            "credential=codex-example-shared-api-key"
            "&secret_value=not-a-real-secret"
            "&overwrite=yes"
        )

        connection.request(
            "POST",
            "/auth/secret",
            body=body.encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()

        assert response.status == 303
        secret_path = tmp_path / "state" / "secrets" / "codex-example-shared-api-key"
        assert secret_path.read_text(encoding="utf-8") == "not-a-real-secret"

        connection.request("GET", "/auth/credentials")
        response = connection.getresponse()
        html = response.read().decode("utf-8")

        assert response.status == 200
        assert "not-a-real-secret" not in html
        assert "codex-example-shared-api-key" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_starts_codex_oauth_device_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )

    class FakeProcess:
        stdout = iter(["Open https://example/device\n", "Use code ABCD-EFGH\n"])

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(controller_auth.shutil, "which", lambda command: "codex")
    monkeypatch.setattr(
        controller_auth.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )

    session = service.start_codex_oauth_login("codex-product-oauth")
    deadline = time.time() + 5
    while session.status == "running" and time.time() < deadline:
        time.sleep(0.01)

    assert session.status == "completed"
    assert session.login_url == "https://example/device"
    assert session.user_code == "ABCD-EFGH"
    assert session.output == ["Open https://example/device", "Use code ABCD-EFGH"]
    assert session.codex_home == tmp_path / "state" / "worker_mounts" / "codex-product-home"


def test_controller_auth_parses_ansi_colored_codex_device_output() -> None:
    assert (
        controller_auth._first_url(
            "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m"
        )
        == "https://auth.openai.com/codex/device"
    )
    assert (
        controller_auth._first_device_code(
            "   \x1b[94m2A4N-771S5\x1b[0m"
        )
        == "2A4N-771S5"
    )


def test_controller_auth_oauth_page_has_openai_button(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(controller_auth.shutil, "which", lambda command: None)
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request("GET", "/auth/credentials")
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Sign in with OpenAI" in body
        assert "CODEX_HOME" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_controller_auth_session_page_polls_without_meta_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_file = tmp_path / "project.yaml"
    project_file.write_text(
        """
project_id: oauth-example
name: OAuth Example
workspace:
  root: .
  default_repository: oauth-example
  repositories:
    oauth-example:
      type: git
      path: .
auth_credentials:
  codex-product-oauth:
    method: codex_oauth_cache
    mount_ref: codex-product-home
roles:
  product-manager:
    template: product-manager
    instances: 1
    worker:
      adapter: codex-cli
      model: codex
      auth:
        credential: codex-product-oauth
    instructions: []
    write_paths: []
    channels: {}
flow:
  flow_id: oauth-example-flow
  entry_state: product_definition
  work_item_types:
    - slice
  states:
    product_definition:
      owner_role: product-manager
      purpose: Define work.
      artifact_path: docs/product/stories.md
      handoffs: {}
""".strip(),
        encoding="utf-8",
    )

    class FakeProcess:
        stdout = iter(
            [
                "Open \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n",
                "Use code \x1b[94m2A4N-771S5\x1b[0m\n",
            ]
        )

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(controller_auth.shutil, "which", lambda command: "codex")
    monkeypatch.setattr(
        controller_auth.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    service = ControllerAuthService(
        config_root=Path.cwd(),
        project_file=str(project_file),
        state_root=tmp_path / "state",
    )
    server = ControllerAuthServer(("127.0.0.1", 0), ControllerAuthHandler, service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=5)

        connection.request(
            "POST",
            "/auth/codex/start",
            body=b"credential=codex-product-oauth",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 303
        location = response.headers["Location"]
        session_id = location.rsplit("id=", 1)[1]

        deadline = time.time() + 5
        session = service.session(session_id)
        assert session is not None
        while session.status == "running" and time.time() < deadline:
            time.sleep(0.01)

        connection.request("GET", location)
        response = connection.getresponse()
        body = response.read().decode("utf-8")

        assert response.status == 200
        assert "http-equiv=\"refresh\"" not in body
        assert "id=\"copy-code\"" in body
        assert "<svg" in body
        assert "2A4N-771S5" in body

        connection.request("GET", f"/auth/codex/session.json?id={session_id}")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 200
        assert payload["login_url"] == "https://auth.openai.com/codex/device"
        assert payload["user_code"] == "2A4N-771S5"
        assert payload["redacted"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
