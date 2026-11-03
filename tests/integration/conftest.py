"""Fixtures for disposable PostgreSQL integration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest


@pytest.fixture
def isolated_postgres_url() -> Iterator[str]:
    """Yield a per-test PostgreSQL schema or skip without a test database URL."""
    database_url = os.environ.get("WEAVES_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set WEAVES_TEST_DATABASE_URL to run PostgreSQL integration tests")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"weaves_it_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_url = make_conninfo(
        database_url, options=f"-c search_path={schema},public"
    )
    try:
        yield isolated_url
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
