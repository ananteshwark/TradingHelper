"""Local mirror must preserve the last good copy and isolate research jobs."""

from __future__ import annotations

import json

import pytest

from igs.local_research import jobs, sync
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


def test_research_artifacts_cannot_escape_workspace():
    with pytest.raises(ValueError, match="Invalid artifact name"):
        jobs.artifact("features", "../../.env")
    assert jobs.artifact("features", "20261107T010000000000Z").is_relative_to(jobs.ARTIFACTS)
