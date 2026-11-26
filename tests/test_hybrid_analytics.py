"""Local-first research handoff: costed report, freshness and fallback arbitration."""

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

from igs import hybrid_analytics as h

GATE = Path(__file__).parents[1] / "scripts" / "research-ssh-gate.sh"


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args):
        assert "net_inr is not null" in sql
        assert args == (dt.date(2026, 11, 7),)
        return FakeCursor(self.rows if "ml_intraday_pick" in sql else [])


def test_costed_side_specific_report_and_feature_categories():
    rows = []
    for pnl, side, offset in ((100, 1, 3), (90, 1, 4), (80, 1, 5),
                              (-30, 1, 1), (-40, 1, 2), (-50, 1, 3),
                              (20, -1, 8)):
        rows.append((dt.date(2026, 11, 6), side, pnl,
                     {"prev_r1": offset, "n_market": offset * 2,
                      "stock_volume": offset * 3, "missing": None}))
    item = h.report(FakeConnection(rows), dt.date(2026, 11, 7), "server")
    buy = item["tracks"]["morning"]["buy"]
    short = item["tracks"]["morning"]["short"]
    assert buy["closed"] == 6 and buy["winners"] == 3 and buy["net_inr"] == 150
    assert short["closed"] == 1 and short["feature_gaps"] == []
    assert {g["category"] for g in buy["feature_gaps"]} == {
        "stock", "previous_session", "market"
    }
    assert item["tracks"]["evening"]["buy"]["closed"] == 0


def test_only_a_fresh_local_report_suppresses_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "OUTPUT", tmp_path)
    day = dt.date(2026, 11, 7)
    fresh = {"schema_version": 1, "analysis_sha256": h.ANALYSIS_SHA256,
             "report_date": str(day), "source": "local",
             "generated_at": "2026-11-07T15:00:00+00:00",
             "mirror_synced_at": "2026-11-07T13:30:00+00:00", "tracks": {}}
    stale = {**fresh, "mirror_synced_at": "2026-11-06T13:30:00+00:00"}
    assert h.valid_local(fresh, day)
    assert not h.valid_local(stale, day)
    assert h.save(fresh, day) == str(tmp_path / f"{day}.json")
    assert h.save({**fresh, "source": "server"}, day) == "already completed"
    assert json.loads((tmp_path / f"{day}.json").read_text())["source"] == "local"
    (tmp_path / f"{day}.json").write_text(json.dumps(stale))
    assert h.save({**fresh, "source": "server"}, day) == str(tmp_path / f"{day}.json")
    assert json.loads((tmp_path / f"{day}.json").read_text())["source"] == "server"
    assert h.save(fresh, day) == "already completed"


def test_mirror_freshness_uses_india_date_and_rejects_missing_sync():
    day = dt.date(2026, 11, 7)
    assert h.mirror_fresh({"state": "ready", "synced_at": "2026-11-06T19:30:00Z"}, day)
    assert not h.mirror_fresh({"state": "ready", "synced_at": "2026-11-06T13:30:00Z"}, day)
    assert not h.mirror_fresh({"state": "failed"}, day)


def test_missing_mirror_status_allows_initial_refresh(tmp_path):
    assert h.read_mirror_state(tmp_path / "missing.json") == {}


def test_receive_rejects_wrong_date_and_oversized_input(tmp_path, monkeypatch):
    import io
    import sys

    monkeypatch.setattr(h, "OUTPUT", tmp_path)
    monkeypatch.setattr(h, "today", lambda: dt.date(2026, 11, 7))
    item = {"schema_version": 1, "analysis_sha256": h.ANALYSIS_SHA256,
            "report_date": "2026-11-06", "source": "local",
            "generated_at": "2026-11-07T15:00:00+00:00",
            "mirror_synced_at": "2026-11-07T13:30:00+00:00", "tracks": {}}
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(item).encode())))
    import pytest

    with pytest.raises(ValueError, match="freshness"):
        h.receive(dt.date(2026, 11, 7))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"x" * (h.MAX_UPLOAD + 1))))
    with pytest.raises(ValueError, match="1 MiB"):
        h.receive(dt.date(2026, 11, 7))


def test_ssh_gate_rejects_shells_and_invalid_upload_paths():
    for command in ("", "bash", "upload ../../etc/passwd", "upload 2026-11-07 extra"):
        result = subprocess.run(
            ["bash", str(GATE)], env={**os.environ, "SSH_ORIGINAL_COMMAND": command},
            capture_output=True, check=False,
        )
        assert result.returncode == 2
