from __future__ import annotations

import os

import psycopg
import pytest

from igs.db import migrate


@pytest.fixture
def db_conn():
    """A freshly migrated schema in the test database; dropped afterwards.

    Set IGS_TEST_DATABASE_URL (e.g. postgresql://igs:igs@localhost:5432/igs_test).
    """
    url = os.environ.get("IGS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("IGS_TEST_DATABASE_URL not set")
    conn = psycopg.connect(url)
    dbname = conn.info.dbname
    if "test" not in dbname:
        conn.close()
        pytest.fail(f"refusing to wipe database {dbname!r}: name must contain 'test'")
    with conn.cursor() as cur:
        cur.execute("drop schema if exists public cascade; create schema public;")
    conn.commit()
    migrate(conn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
