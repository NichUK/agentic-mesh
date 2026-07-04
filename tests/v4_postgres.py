from __future__ import annotations

import os
import uuid
from urllib.parse import quote

import pytest

from agentic_mesh_v4.db import V4Database


def make_v4_db_url() -> str:
    base_url = os.environ.get("AGENTIC_MESH_TEST_DATABASE_URL")
    if not base_url:
        pytest.skip("AGENTIC_MESH_TEST_DATABASE_URL is required for V4 Postgres tests")
    schema = f"test_{uuid.uuid4().hex}"
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        pytest.skip("psycopg is required for V4 Postgres tests")
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}options={quote(f'-csearch_path={schema}')}"


def make_v4_db() -> V4Database:
    db = V4Database(make_v4_db_url())
    db.migrate()
    return db
