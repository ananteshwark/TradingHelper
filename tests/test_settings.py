"""The Settings page and what it writes: assistant settings to a local overrides file
(never the tracked config) and the API key to .env."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit
import yaml
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from igs import envfile, settings
from igs.config import load_assistant, settings_dir

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"


def _overrides() -> dict:
    return yaml.safe_load(settings.assistant_path().read_text(encoding="utf-8"))


def test_only_changed_values_are_stored_and_merged_over_the_defaults():
    base = load_assistant()
    assert not settings.assistant_path().exists() and base.enabled is False
    settings.save_assistant({"enabled": True, "model": base.model,
                             "features": {"ask": {"effort": "low",
                                                  "max_tool_rounds": base.features.ask
                                                  .max_tool_rounds}}})
    assert _overrides() == {"enabled": True, "features": {"ask": {"effort": "low"}}}
    cfg = load_assistant()
    assert cfg.enabled and cfg.features.ask.effort == "low"
    assert cfg.features.brief == base.features.brief            # untouched defaults apply
    assert settings.assistant_path().parent == settings_dir()
    with pytest.raises(ValidationError, match="no price"):
        settings.save_assistant({"model": "claude-unpriced"})
    assert _overrides() == {"enabled": True, "features": {"ask": {"effort": "low"}}}
    settings.save_assistant({"enabled": False})                 # back to the defaults
    assert not settings.assistant_path().exists()
    settings.save_assistant({"daily_budget_usd": 5.0})
    settings.reset_assistant()
    assert load_assistant() == base


def test_api_key_is_written_to_env_privately_without_touching_other_lines(tmp_path,
                                                                            monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# settings\nIGS_DATABASE_URL=postgresql://x\nANTHROPIC_API_KEY=old\n",
                   encoding="utf-8")
    envfile.set_value(env, "ANTHROPIC_API_KEY", "sk-ant-new")
    assert env.read_text(encoding="utf-8") == (
        "# settings\nIGS_DATABASE_URL=postgresql://x\nANTHROPIC_API_KEY=sk-ant-new\n")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-new"
    if os.name == "posix":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="single word"):
        envfile.set_value(env, "ANTHROPIC_API_KEY", "two words")
    envfile.unset(env, "ANTHROPIC_API_KEY")
    assert "ANTHROPIC_API_KEY" not in env.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert envfile.default_path() == Path(os.environ["IGS_ENV_FILE"])


def test_connection_check_uses_the_models_api():
    from igs.assistant.llm import check_connection
    fake = SimpleNamespace(models=SimpleNamespace(
        retrieve=lambda model: SimpleNamespace(id=model, display_name="Claude Opus 5")))
    assert check_connection(load_assistant(), fake) == "Claude Opus 5 (claude-opus-5)"


# --------------------------------------------------------------------------- the page


@pytest.fixture
def page(db_conn, monkeypatch):
    """The Settings page, served as `igs ui` serves it (this computer only)."""
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.sidebar.radio(key="page").set_value("Settings").run()
    assert not at.exception, at.exception
    return at


@pytest.mark.db
def test_settings_page_saves_assistant_settings(page):
    at = page
    assert any(h.value == "Settings" for h in at.header)
    at.toggle(key="set_enabled").set_value(True)
    at.selectbox(key="set_model").select("claude-sonnet-5")
    at.number_input(key="set_budget").set_value(5.0)
    at.selectbox(key="set_ann_scope").select("watchlist")
    next(b for b in at.button if b.label == "Save settings").click().run()
    assert not at.exception, at.exception
    assert _overrides() == {"enabled": True, "model": "claude-sonnet-5",
                            "daily_budget_usd": 5.0,
                            "features": {"announcements": {"scope": "watchlist"}}}
    assert any("Settings saved" in s.value for s in at.success)
    assert any("no API key is set" in w.value for w in at.warning)
    cfg = load_assistant()
    assert cfg.enabled and cfg.model == "claude-sonnet-5"


@pytest.mark.db
def test_settings_page_saves_and_removes_the_api_key(page):
    at = page
    env = Path(os.environ["IGS_ENV_FILE"])
    at.text_input(key="set_key").input("sk-ant-api03-abcdefghijklmnop").run()
    at.button(key="set_key_save").click().run()
    assert not at.exception, at.exception
    assert env.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnop\n"
    assert at.text_input(key="set_key").value == ""                # not left in the field
    shown = " ".join(m.value for m in at.markdown)
    assert "sk-ant-...mnop" in shown and "abcdefghijkl" not in shown
    at.button(key="set_key_remove").click().run()
    assert "ANTHROPIC_API_KEY" not in env.read_text(encoding="utf-8")
    assert "not set" in " ".join(m.value for m in at.markdown)


@pytest.mark.db
def test_settings_are_read_only_when_the_ui_is_exposed(db_conn, monkeypatch):
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "0.0.0.0"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.sidebar.radio(key="page").set_value("Settings").run()
    assert not at.exception, at.exception
    assert any("can't be changed here" in w.value for w in at.warning)
    assert at.toggle(key="set_enabled").disabled and at.text_input(key="set_key").disabled
