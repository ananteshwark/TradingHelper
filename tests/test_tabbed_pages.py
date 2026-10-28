"""The Trading and Paper tracking pages: each shows several pages as tabs, and only the open
tab runs."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
APP = Path(__file__).resolve().parents[1] / "src/igs/ui/app.py"


def _captions(at) -> str:
    return " ".join(c.value for c in at.caption)


@pytest.mark.db
def test_paper_tracking_and_trading_pages_show_one_tab_at_a_time(db_conn, monkeypatch):
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    at = st_testing.AppTest.from_file(str(APP), default_timeout=60).run()
    pages = at.sidebar.radio(key="page").options
    assert "Trading" in pages and "Paper tracking" in pages
    assert not {"Intraday calls", "ML intraday", "The Plan (paper)", "Momentum (paper)",
                "Results days (paper)"} & set(pages)

    at.sidebar.radio(key="page").set_value("Paper tracking").run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == ["The Plan", "Momentum", "Results days",
                                          "Spike reversal"]
    assert [h.value for h in at.header] == ["Paper tracking"]      # the tab names the page
    # The Plan's tab is open; the other pages don't run.
    assert "The plan keeps" in _captions(at)
    assert not any("No portfolio yet" in i.value for i in at.info)
    at.session_state["paper_tab"] = "Momentum"
    at.run()
    assert not at.exception, at.exception
    assert any("No portfolio yet" in i.value for i in at.info)
    assert "The plan keeps" not in _captions(at)
    at.session_state["paper_tab"] = "Results days"
    at.run()
    assert not at.exception, at.exception
    assert any(i.value.startswith("None yet: the daily job records") for i in at.info)
    at.session_state["paper_tab"] = "Spike reversal"
    at.run()
    assert not at.exception, at.exception
    assert any(i.value.startswith("No spike today yet") for i in at.info)
    assert "too early: 0 of 100 trades" in " ".join(m.value for m in at.markdown)

    at.sidebar.radio(key="page").set_value("Trading").run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == ["Intraday calls", "ML intraday", "Evening picks",
                                          "Open-trade monitor"]
    assert "The record below is on paper. At 09:45" not in _captions(at)
    at.session_state["trading_tab"] = "ML intraday"
    at.run()
    assert not at.exception, at.exception
    assert "The record below is on paper. At 09:45" in _captions(at)
    at.session_state["trading_tab"] = "Evening picks"
    at.run()
    assert not at.exception, at.exception
    assert any(i.value.startswith("No calls yet: the model runs after the close")
               for i in at.info)
    at.session_state["trading_tab"] = "Open-trade monitor"
    at.run()
    assert not at.exception, at.exception
    assert any("No app trades today" in i.value for i in at.info)
