from __future__ import annotations

import importlib.util
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


@pytest.fixture(autouse=True)
def _local_settings_isolated(tmp_path, monkeypatch):
    """Settings saved from the UI (data/settings) and the developer's .env must not leak
    into tests; each test gets empty ones."""
    monkeypatch.setenv("IGS_AUTH_MODE", "local")
    monkeypatch.setenv("IGS_API_TOKEN", "test-api-token-" + "x" * 32)
    if importlib.util.find_spec("streamlit") is not None:
        import streamlit
        original_option = streamlit.get_option
        monkeypatch.setattr(streamlit, "get_option", lambda key: "127.0.0.1"
                            if key == "server.address" else original_option(key))
    monkeypatch.setenv("IGS_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("IGS_ENV_FILE", str(tmp_path / "test.env"))
    for key in ("IGS_WHATSAPP_PROVIDER", "IGS_WHATSAPP_TO", "IGS_WHATSAPP_TOKEN",
                "IGS_WHATSAPP_PHONE_ID", "IGS_CALLMEBOT_APIKEY", "IGS_TELEGRAM_TOKEN",
                "IGS_TELEGRAM_CHAT_ID", "IGS_SMTP_HOST"):         # never message anyone
        monkeypatch.delenv(key, raising=False)
