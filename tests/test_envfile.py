"""The .env settings file and the CLI's local-only defaults."""

from __future__ import annotations

import os

import pytest

from igs import cli, envfile


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


def test_ui_listens_on_this_computer_only_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.call", lambda cmd, cwd=None: calls.append(cmd) or 0)
    assert cli._ui(cli.build_parser().parse_args(["ui"])) == 0
    cmd = calls[0]
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--server.port") + 1] == "8501"
    assert cli.build_parser().parse_args(["api"]).host == "127.0.0.1"
