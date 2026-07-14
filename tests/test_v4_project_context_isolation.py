from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterator
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from uuid import uuid4

import pytest

from agentic_mesh_v4.codex_protocol import CodexAppServerClient
from agentic_mesh_v4.codex_protocol import InMemoryTransport
from agentic_mesh_v4.compose import render_compose
from agentic_mesh_v4.config import V4ProjectConfig
from agentic_mesh_v4.config import V4RoleConfig
from agentic_mesh_v4.db import V4Database
from agentic_mesh_v4.runtime import V4Runtime


LOGICAL_ROLE_ID = "project-manager"
AGENT_NETWORK_ID = "synthetic-shared-agent"


@dataclass(frozen=True)
class PostgresProject:
    project_id: str
    schema: str
    role: str
    database_url: str


def test_postgres_runtime_isolates_orchid_cedar_orchid_context_and_memory() -> None:
    with _postgres_projects("synthetic-orchid", "synthetic-cedar") as (orchid_pg, cedar_pg):
        orchid_db = V4Database(orchid_pg.database_url)
        cedar_db = V4Database(cedar_pg.database_url)
        try:
            orchid_db.migrate()
            cedar_db.migrate()
            orchid_config = _project_config(orchid_pg.project_id)
            cedar_config = _project_config(cedar_pg.project_id)
            orchid_transport = InMemoryTransport()
            cedar_transport = InMemoryTransport()
            orchid_client = CodexAppServerClient(orchid_transport)
            cedar_client = CodexAppServerClient(cedar_transport)
            orchid_runtime = V4Runtime(
                db=orchid_db,
                project_config=orchid_config,
                client_factory=lambda _role_id: orchid_client,
            )
            cedar_runtime = V4Runtime(
                db=cedar_db,
                project_config=cedar_config,
                client_factory=lambda _role_id: cedar_client,
            )
            orchid_runtime.register_roles()
            cedar_runtime.register_roles()

            orchid_instance = orchid_config.role_instance_id(LOGICAL_ROLE_ID)
            cedar_instance = cedar_config.role_instance_id(LOGICAL_ROLE_ID)
            assert orchid_instance == "synthetic-orchid.project-manager.1"
            assert cedar_instance == "synthetic-cedar.project-manager.1"
            assert orchid_config.role(LOGICAL_ROLE_ID).service_name == cedar_config.role(LOGICAL_ROLE_ID).service_name

            orchid_message_1 = orchid_runtime.enqueue_conversation(
                target_role=LOGICAL_ROLE_ID,
                text="orchid-one",
            )
            _queue_first_turn(orchid_transport, thread_id="thread-orchid", turn_id="turn-orchid-1")
            orchid_result_1 = orchid_runtime.dispatch_once(
                project_id="synthetic-orchid",
                role_id=LOGICAL_ROLE_ID,
            )
            orchid_db.upsert_work_item(
                work_item_id="orchid-state",
                title="Orchid state",
                state="preserved",
                owner_role=LOGICAL_ROLE_ID,
                next_action="resume-orchid",
            )
            orchid_db.record_project_memory(
                project_id="synthetic-orchid",
                summary="orchid-project-memory",
                source_ref="synthetic-orchid/project-memory",
                created_by_role_instance_id=orchid_instance,
            )
            orchid_db.record_memory(
                role_instance_id=orchid_instance,
                project_id="synthetic-orchid",
                summary="orchid-role-memory",
                source_ref="synthetic-orchid/role-memory",
            )

            cedar_message = cedar_runtime.enqueue_conversation(
                target_role=LOGICAL_ROLE_ID,
                text="cedar-one",
            )
            _queue_first_turn(cedar_transport, thread_id="thread-cedar", turn_id="turn-cedar-1")
            cedar_result = cedar_runtime.dispatch_once(
                project_id="synthetic-cedar",
                role_id=LOGICAL_ROLE_ID,
            )
            cedar_db.record_project_memory(
                project_id="synthetic-cedar",
                summary="cedar-project-memory",
                source_ref="synthetic-cedar/project-memory",
                created_by_role_instance_id=cedar_instance,
            )
            cedar_db.record_memory(
                role_instance_id=cedar_instance,
                project_id="synthetic-cedar",
                summary="cedar-role-memory",
                source_ref="synthetic-cedar/role-memory",
            )

            orchid_message_2 = orchid_runtime.enqueue_conversation(
                target_role=LOGICAL_ROLE_ID,
                text="orchid-two",
            )
            _queue_resumed_turn(orchid_transport, thread_id="thread-orchid", turn_id="turn-orchid-2")
            orchid_result_2 = orchid_runtime.dispatch_once(
                project_id="synthetic-orchid",
                role_id=LOGICAL_ROLE_ID,
            )

            assert orchid_result_1 is not None and orchid_result_1.thread_id == "thread-orchid"
            assert cedar_result is not None and cedar_result.thread_id == "thread-cedar"
            assert orchid_result_2 is not None and orchid_result_2.thread_id == "thread-orchid"
            assert any(
                item.get("method") == "thread/resume"
                and item.get("params", {}).get("threadId") == "thread-orchid"
                for item in orchid_transport.sent
            )

            assert _column(orchid_db, "message_queue", "message_id") == [orchid_message_1, orchid_message_2]
            assert _column(cedar_db, "message_queue", "message_id") == [cedar_message]
            assert _column(orchid_db, "codex_threads", "thread_id") == ["thread-orchid"]
            assert _column(cedar_db, "codex_threads", "thread_id") == ["thread-cedar"]
            assert _column(orchid_db, "project_memory", "summary") == ["orchid-project-memory"]
            assert _column(cedar_db, "project_memory", "summary") == ["cedar-project-memory"]
            assert _column(orchid_db, "role_memory", "summary") == ["orchid-role-memory"]
            assert _column(cedar_db, "role_memory", "summary") == ["cedar-role-memory"]
            assert orchid_db.connection.execute(
                "SELECT state, next_action FROM work_items WHERE work_item_id=?",
                ("orchid-state",),
            ).fetchone() == {"state": "preserved", "next_action": "resume-orchid"}
            assert _count(orchid_db, "codex_turns") == 2
            assert _count(cedar_db, "codex_turns") == 1
            assert _count(orchid_db, "agent_events") > _count(cedar_db, "agent_events")

            pending = orchid_runtime.enqueue_conversation(target_role=LOGICAL_ROLE_ID, text="stay-queued")
            with pytest.raises(ValueError, match="project context is required"):
                orchid_runtime.dispatch_once(role_id=LOGICAL_ROLE_ID)
            with pytest.raises(ValueError, match="project context mismatch"):
                orchid_runtime.dispatch_once(project_id="synthetic-cedar", role_id=LOGICAL_ROLE_ID)
            assert orchid_db.connection.execute(
                "SELECT state, locked_by, delivery_attempts FROM message_queue WHERE message_id=?",
                (pending,),
            ).fetchone() == {"state": "queued", "locked_by": None, "delivery_attempts": 0}

            _assert_cross_schema_denied(orchid_pg, forbidden_schema=cedar_pg.schema)
            _assert_cross_schema_denied(cedar_pg, forbidden_schema=orchid_pg.schema)
        finally:
            orchid_db.close()
            cedar_db.close()


def test_live_compose_mounts_are_isolated_orchid_cedar_orchid() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for live mount isolation")
    image = os.environ.get("AGENTIC_MESH_LIVE_ISOLATION_IMAGE", "agentic-mesh:ops-agent")
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspect.returncode != 0:
        pytest.skip(f"live isolation image is unavailable: {image}")

    live_root = Path.cwd() / ".tmp" / f"project-context-isolation-{uuid4().hex[:12]}"
    try:
        orchid = _live_project(live_root / "orchid", "synthetic-orchid")
        cedar = _live_project(live_root / "cedar", "synthetic-cedar")
        orchid_first = _run_live_mount_probe(orchid, forbidden_project_id="synthetic-cedar", image=image)
        cedar_result = _run_live_mount_probe(cedar, forbidden_project_id="synthetic-orchid", image=image)
        orchid_return = _run_live_mount_probe(orchid, forbidden_project_id="synthetic-cedar", image=image)

        assert orchid_first == orchid_return
        assert orchid_first["project_id"] == "synthetic-orchid"
        assert cedar_result["project_id"] == "synthetic-cedar"
        assert orchid_first["role_instance_id"] == "synthetic-orchid.project-manager.1"
        assert cedar_result["role_instance_id"] == "synthetic-cedar.project-manager.1"
        assert set(orchid_first["mounts"]) == {"document", "project", "repository", "workspace"}
        assert set(cedar_result["mounts"]) == {"document", "project", "repository", "workspace"}
    finally:
        shutil.rmtree(live_root, ignore_errors=True)


@contextmanager
def _postgres_projects(*project_ids: str) -> Iterator[tuple[PostgresProject, ...]]:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for PostgreSQL isolation")
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        pytest.skip("psycopg is required for PostgreSQL isolation")

    suffix = uuid4().hex[:12]
    projects: list[PostgresProject] = []
    with psycopg.connect(base_url, autocommit=True) as admin:
        for index, project_id in enumerate(project_ids):
            role = f"isolation_{index}_{suffix}"
            schema = f"isolation_{index}_{suffix}"
            password = uuid4().hex
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role),
                    sql.Literal(password),
                )
            )
            admin.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(role),
                )
            )
            admin.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
            projects.append(
                PostgresProject(
                    project_id=project_id,
                    schema=schema,
                    role=role,
                    database_url=_credential_url(base_url, role=role, password=password, schema=schema),
                )
            )
    try:
        yield tuple(projects)
    finally:
        with psycopg.connect(base_url, autocommit=True) as admin:
            for project in projects:
                admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(project.schema)))
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(project.role)))


def _credential_url(base_url: str, *, role: str, password: str, schema: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    return urlunsplit(
        (
            parsed.scheme,
            f"{quote(role, safe='')}:{quote(password, safe='')}@{host}",
            parsed.path,
            urlencode(query),
            "",
        )
    )


def _assert_cross_schema_denied(project: PostgresProject, *, forbidden_schema: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(project.database_url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                sql.SQL("SELECT memory_id FROM {}.project_memory").format(sql.Identifier(forbidden_schema))
            ).fetchall()


def _project_config(project_id: str) -> V4ProjectConfig:
    role = V4RoleConfig(
        role_id=LOGICAL_ROLE_ID,
        display_name="Project Manager",
        template=LOGICAL_ROLE_ID,
        agent_network_id=AGENT_NETWORK_ID,
        authority="full",
        sandbox_mode="danger-full-access",
        approval_policy="never",
        codex_port=4700,
    )
    return V4ProjectConfig(
        project_id=project_id,
        agent_network_id=AGENT_NETWORK_ID,
        name=project_id,
        goal="Synthetic project-context isolation",
        roles=(role,),
    )


def _queue_first_turn(transport: InMemoryTransport, *, thread_id: str, turn_id: str) -> None:
    transport.queue_response({"id": 1, "result": {}})
    transport.queue_response(None)
    transport.queue_response({"id": 2, "result": {"thread": {"id": thread_id}}})
    transport.queue_response({"id": 3, "result": {"turn": {"id": turn_id}}})
    transport.queue_notification({"method": "turn/completed", "params": {}})


def _queue_resumed_turn(transport: InMemoryTransport, *, thread_id: str, turn_id: str) -> None:
    transport.queue_response({"id": 4, "result": {"thread": {"id": thread_id}}})
    transport.queue_response({"id": 5, "result": {"turn": {"id": turn_id}}})
    transport.queue_notification({"method": "turn/completed", "params": {}})


def _column(db: V4Database, table: str, column: str) -> list[str]:
    rows = db.connection.execute(f"SELECT {column} FROM {table} ORDER BY created_at").fetchall()
    return [str(row[column]) for row in rows]


def _count(db: V4Database, table: str) -> int:
    return int(db.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])


def _live_project(root: Path, project_id: str) -> dict[str, Path | str]:
    project_root = root / "project"
    documents = root / "documents"
    repository = root / "repository"
    agent_workspace = project_root / "state" / "v4" / "agent-workspaces" / LOGICAL_ROLE_ID / "1"
    agent_config = project_root / "state" / "v4" / "agent-configs" / LOGICAL_ROLE_ID / "1"
    codex_home = root / "codex-home"
    ssh_root = root / "ssh"
    for path in (project_root / "state", documents, repository, agent_workspace, agent_config, codex_home, ssh_root):
        path.mkdir(parents=True, exist_ok=True)
    (documents / f"{project_id}.marker").write_text("document", encoding="utf-8")
    (repository / f"{project_id}.marker").write_text("repository", encoding="utf-8")
    (agent_workspace / f"{project_id}.marker").write_text("workspace", encoding="utf-8")
    (project_root / f"{project_id}.marker").write_text("project", encoding="utf-8")
    (agent_config / "AGENTS.md").write_text("# Synthetic role\n", encoding="utf-8")
    (agent_config / "ws-token").write_text("synthetic-token", encoding="utf-8")
    env_file = root / ".env"
    env_file.write_text("", encoding="utf-8")
    config = _project_config(project_id)
    compose_file = root / "compose.yaml"
    compose_file.write_text(render_compose(config), encoding="utf-8")
    return {
        "project_id": project_id,
        "project_root": project_root,
        "documents": documents,
        "repository": repository,
        "codex_home": codex_home,
        "ssh_root": ssh_root,
        "env_file": env_file,
        "compose_file": compose_file,
        "service_name": config.role(LOGICAL_ROLE_ID).service_name,
    }


def _run_live_mount_probe(
    project: dict[str, Path | str],
    *,
    forbidden_project_id: str,
    image: str,
) -> dict[str, object]:
    project_id = str(project["project_id"])
    compose_file = Path(project["compose_file"])
    project_name = f"isolation-{project_id}"
    environment = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project_name,
        "AGENTIC_MESH_OPS_IMAGE_TAG": image,
        "AGENTIC_MESH_PROJECT_HOST_PATH": str(_docker_host_path(Path(project["project_root"]))),
        "AGENTIC_MESH_DOCUMENTS_HOST_PATH": str(_docker_host_path(Path(project["documents"]))),
        "AGENTIC_MESH_WORKSPACE_HOST_PATH": str(_docker_host_path(Path(project["repository"]))),
        "AGENTIC_MESH_CODEX_HOME_HOST_PATH": str(_docker_host_path(Path(project["codex_home"]))),
        "AGENTIC_MESH_SYSTEM_HOST_PATH": str(_docker_host_path(Path(__file__).parents[1])),
        "AGENTIC_MESH_PROJECT_ENV_FILE_HOST_PATH": str(_docker_host_path(Path(project["env_file"]))),
        "AGENTIC_MESH_PROJECT_MANAGER_SSH_HOST_PATH": str(_docker_host_path(Path(project["ssh_root"]))),
    }
    probe = """
import json
import os
from pathlib import Path
import sys

expected, forbidden = sys.argv[1:3]
paths = {
    "document": Path("/documents") / f"{expected}.marker",
    "project": Path("/mesh/project") / f"{expected}.marker",
    "repository": Path("/mesh/workspaces/agentic-mesh") / f"{expected}.marker",
    "workspace": Path("/mesh/agent-workspace") / f"{expected}.marker",
}
for name, path in paths.items():
    if not path.is_file():
        raise SystemExit(f"missing {name}: {path}")
for root in ("/documents", "/mesh/project", "/mesh/workspaces/agentic-mesh", "/mesh/agent-workspace"):
    if (Path(root) / f"{forbidden}.marker").exists():
        raise SystemExit(f"cross-project marker visible in {root}")
print(json.dumps({
    "project_id": expected,
    "role_instance_id": os.environ["AGENTIC_MESH_ROLE_INSTANCE_ID"],
    "mounts": sorted(paths),
}, sort_keys=True))
"""
    command = [
        "docker",
        "compose",
        "--profile",
        "roles",
        "-f",
        str(compose_file),
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        str(project["service_name"]),
        "-c",
        probe,
        project_id,
        forbidden_project_id,
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, env=environment)
        if result.returncode != 0:
            raise AssertionError(
                f"live mount probe failed for {project_id}: "
                f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
            )
        return json.loads(result.stdout.strip().splitlines()[-1])
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down", "--remove-orphans"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )


def _docker_host_path(path: Path) -> Path:
    resolved = path.resolve()
    inspect = subprocess.run(
        ["docker", "inspect", os.uname().nodename, "--format", "{{json .Mounts}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    mounts = sorted(json.loads(inspect.stdout), key=lambda item: len(item["Destination"]), reverse=True)
    for mount in mounts:
        destination = Path(mount["Destination"])
        try:
            relative = resolved.relative_to(destination)
        except ValueError:
            continue
        return Path(mount["Source"]) / relative
    raise AssertionError(f"live probe path is not backed by a Docker host bind mount: {resolved}")
