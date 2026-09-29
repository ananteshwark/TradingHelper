"""The .env settings file and the CLI's local-only defaults."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from igs import cli, envfile

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


def test_parse_accepts_the_documented_format():
    text = ("# settings\n\nIGS_DATABASE_URL=postgresql://igs:p@ss=word@localhost:5432/igs\n"
            "export IGS_ALERT_TO = me@example.com \nIGS_TELEGRAM_CHAT_ID=\"-100123\"\n"
            "IGS_SMTP_PASSWORD='two words'\n")
    assert envfile.parse(text) == {
        "IGS_DATABASE_URL": "postgresql://igs:p@ss=word@localhost:5432/igs",
        "IGS_ALERT_TO": "me@example.com", "IGS_TELEGRAM_CHAT_ID": "-100123",
        "IGS_SMTP_PASSWORD": "two words"}
    with pytest.raises(ValueError, match="line 1"):
        envfile.parse("not a setting\n")


def test_load_never_overrides_the_environment(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_bytes("﻿IGS_A=from-file\r\nIGS_B=from-file\r\n".encode())   # Notepad: BOM, CRLF
    monkeypatch.setenv("IGS_A", "from-env")
    monkeypatch.delenv("IGS_B", raising=False)
    assert envfile.load(f) == ["IGS_B"]
    assert (os.environ["IGS_A"], os.environ["IGS_B"]) == ("from-env", "from-file")
    monkeypatch.delenv("IGS_B")
    assert envfile.load(tmp_path / "missing.env") == []


class _Process:
    """A stand-in for a child process that exits after `polls` checks on it."""

    def __init__(self, cmd=None, polls=0):
        self.cmd, self.returncode, self.polls = cmd, None, polls

    def poll(self):
        self.polls -= 1
        if self.polls < 0:
            self.returncode = 0
        return self.returncode

    def wait(self):
        self.returncode = 0
        return 0


def _run_ui(monkeypatch, argv: list[str]) -> tuple[list, list]:
    """`igs ui` with the app and the background checks faked; every 5 s pause of its loop
    moves a fake clock on by 3 hours."""
    import igs.sync
    apps, checks, clock = [], [], [0.0]
    monkeypatch.setattr(cli, "_migrate_on_start", lambda: apps.append("migrated"))
    monkeypatch.setattr(cli, "_port_in_use", lambda host, port: False)
    monkeypatch.setattr("subprocess.Popen",
                        lambda cmd, cwd=None: apps.append(_Process(cmd, polls=2)) or apps[-1])
    monkeypatch.setattr(igs.sync, "start_background_sync",
                        lambda trigger: checks.append(trigger) or _Process())
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__(0, clock[0] + 3 * 3600))
    assert cli._ui(cli.build_parser().parse_args(argv)) == 0
    return apps, checks


def test_ui_listens_on_this_computer_only_by_default(monkeypatch):
    apps, _ = _run_ui(monkeypatch, ["ui"])
    assert apps[0] == "migrated"          # the database is brought up to date first
    cmd = apps[1].cmd
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--server.port") + 1] == "8501"
    assert cli.build_parser().parse_args(["api"]).host == "127.0.0.1"


def test_ui_says_when_the_old_app_still_holds_the_port(monkeypatch, capsys):
    """Seen after an update: the old app kept port 8501, Streamlit printed only "Port 8501
    is not available", and a background NSE check had started for an app that never ran."""
    import socket
    started = []
    monkeypatch.setattr(cli, "_migrate_on_start", lambda: started.append("migrated"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: started.append("app"))
    with socket.socket() as old_app:
        old_app.bind(("127.0.0.1", 0))
        old_app.listen()
        port = old_app.getsockname()[1]
        rc = cli._ui(cli.build_parser().parse_args(["ui", "--port", str(port)]))
    assert rc == 1 and started == []
    err = capsys.readouterr().err
    assert f"Port {port} is in use" in err and "pkill -f" in err


def test_ui_checks_nse_at_start_and_every_interval(monkeypatch):
    _, checks = _run_ui(monkeypatch, ["ui"])
    assert checks == ["startup", "interval"]          # the app ran for about 3 hours
    _, checks = _run_ui(monkeypatch, ["ui", "--no-sync"])
    assert checks == []


@pytest.mark.db
def test_ui_start_applies_what_an_update_added(db_conn, monkeypatch, capsys):
    """After a `git pull` that adds a migration, `igs ui` applies it before the app opens,
    and until then the app says the database is behind."""
    from igs.db import pending_migrations
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    assert pending_migrations(db_conn) == []
    db_conn.execute("drop table ai_call")
    db_conn.execute("delete from schema_migrations where version = '019_ai_calls'")
    db_conn.commit()
    assert pending_migrations(db_conn) == ["019_ai_calls"]

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["page"] = "Settings"
    at.run()
    assert any("019_ai_calls not applied" in e.value for e in at.error)

    cli._migrate_on_start()
    assert "Database updated for this version of the app: 019_ai_calls" in \
        capsys.readouterr().out
    assert pending_migrations(db_conn) == []


def test_a_missing_table_asks_for_the_migration(monkeypatch, capsys):
    import psycopg

    def old_database(args):
        raise psycopg.errors.UndefinedTable('relation "sync_run" does not exist')
    monkeypatch.setattr(cli, "_sync", old_database)
    assert cli.main(["sync"]) == 2
    assert "run `uv run igs db migrate`" in capsys.readouterr().err


# --------------------------------------------------------------------------- DB errors


def test_connection_errors_are_explained_without_the_password(monkeypatch, capsys):
    from igs import cli
    from igs.db import connection_help, masked
    assert masked("postgresql://igs:s3cret@localhost:5432/igs") == \
        "postgresql://igs:***@localhost:5432/igs"
    assert masked("postgresql://localhost/igs") == "postgresql://localhost/igs"
    url = "postgresql://igs:s3cret@localhost:5432/igs"
    auth = connection_help(Exception(
        'connection failed: connection to server at "127.0.0.1", port 5432 failed: FATAL:  '
        'password authentication failed for user "igs"'), url, ".env")
    assert auth.startswith('Could not connect to PostgreSQL: password authentication failed')
    assert "ALTER ROLE igs WITH LOGIN PASSWORD" in auth and "s3cret" not in auth
    missing = connection_help(Exception('... failed: FATAL:  database "igs" does not exist'),
                              url, ".env")
    assert "createdb -O igs igs" in missing
    # End to end through the CLI: nothing listens on port 1.
    monkeypatch.setenv("IGS_DATABASE_URL", "postgresql://igs:s3cret@127.0.0.1:1/igs")
    assert cli.main(["db", "migrate"]) == 2
    err = capsys.readouterr().err
    assert "Connection refused" in err and "environment variable" in err
    assert "s3cret" not in err and "Traceback" not in err


def test_documents_before_the_master_stop_with_a_message(monkeypatch, capsys):
    from igs.ingest.jobs import MasterNotBuilt

    def no_master(args):
        raise MasterNotBuilt("3 financial_results documents are waiting ... run "
                             "`igs master rebuild` first.")
    monkeypatch.setattr(cli, "_ingest_documents", no_master)
    assert cli.main(["ingest", "documents", "financial_results"]) == 2
    assert "Stopped: 3 financial_results documents" in capsys.readouterr().err


def test_data_quality_notes_print_only_when_verbose(tmp_path, monkeypatch, caplog):
    import logging

    from igs.dq import DQLog
    monkeypatch.setenv("IGS_RAW_ROOT", str(tmp_path))
    caplog.set_level(logging.DEBUG)          # the root level, as `igs -v` sets it
    dq_log = logging.getLogger("igs.dq")
    try:
        for argv, shown in ((["sources", "list"], False), (["-v", "sources", "list"], True)):
            assert cli.main(argv) == 0
            caplog.clear()
            dq = DQLog()
            dq.emit("info", "xbrl_unmapped_element", "27 numeric elements not mapped")
            dq.emit("error", "filing_unmapped", "RSSOFTWARE: no company in instrument master")
            text = caplog.text
            assert ("27 numeric elements" in text) == shown
            assert "RSSOFTWARE" in text and len(dq.issues) == 2     # always kept
    finally:
        dq_log.setLevel(logging.NOTSET)
