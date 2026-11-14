"""Local mirror must preserve the last good copy and isolate research jobs."""

from __future__ import annotations

import json

import pytest

from igs.local_research import app, jobs, sync
from igs.local_research.app import differences


def test_feature_comparison_keeps_trade_side_separate():
    rows = []
    for side in ("Buy", "Short"):
        for n in range(6):
            rows.append(
                {
                    "track": "Morning ML",
                    "side": side,
                    "net_inr": 100 if n < 3 else -100,
                    "features": {
                        "opening_move": n if side == "Buy" else 100 + n,
                        "missing": None,
                        "flag": True,
                    },
                }
            )
    buy = differences(rows, "Morning ML", "Buy")
    short = differences(rows, "Morning ML", "Short")
    assert len(buy) == len(short) == 1
    assert buy[0]["Winner median"] == 1
    assert short[0]["Winner median"] == 101


def test_failed_refresh_preserves_last_good_mirror(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "STATE", tmp_path)
    sync.status(
        {"state": "ready", "synced_at": "2026-11-07T01:00:00+00:00", "database": "igs_research"}
    )

    def failed(*_args):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(sync, "fetch", failed)
    assert sync.main(["--host", "root@example.test"]) == 1
    state = json.loads((tmp_path / "status.json").read_text())
    assert state["state"] == "ready"
    assert state["synced_at"] == "2026-11-07T01:00:00+00:00"
    assert state["last_sync_error"] == "network unavailable"


def test_manual_sync_starts_the_scheduled_service(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(app.subprocess, "run", run)
    app.start_sync()
    assert calls[0][0] == ["systemctl", "--user", "start", "--no-block", app.SYNC_UNIT]


def test_local_console_can_start_sync_without_a_ready_mirror(monkeypatch):
    from streamlit.testing.v1 import AppTest

    started = []
    monkeypatch.setattr(app, "_status", lambda: {"state": "failed"})
    monkeypatch.setattr(app, "sync_service_state", lambda: "failed")
    monkeypatch.setattr(app, "start_sync", lambda: started.append(True))

    def page():
        from igs.local_research.app import main

        main()

    at = AppTest.from_function(page).run()
    assert not at.exception, at.exception
    at.button(key="manual_sync").click().run()
    assert not at.exception, at.exception
    assert started == [True]
    assert any("Sync started" in item.value for item in at.success)


def test_research_artifacts_cannot_escape_workspace():
    with pytest.raises(ValueError, match="Invalid artifact name"):
        jobs.artifact("features", "../../.env")
    assert jobs.artifact("features", "20261107T010000000000Z").is_relative_to(jobs.ARTIFACTS)
