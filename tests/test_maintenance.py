"""The cleanup of stale data and backups (igs.maintenance, config/maintenance.yaml)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from igs import cli, maintenance
from igs.config import MaintenanceConfig, load_maintenance
from igs.timeutil import IST

TODAY = dt.date(2026, 11, 2)
NOW = dt.datetime(2026, 11, 2, 3, 17, tzinfo=IST)


def _log(path: Path, days: list[int]) -> None:
    """One igs-job.sh entry per day, `days` before TODAY, oldest first."""
    lines = []
    for d in days:
        day = TODAY - dt.timedelta(days=d)
        lines += [f"=== {day} 08:00:00 IST igs news process\n", f"output of day {d}\n",
                  "Traceback: a line with no date\n"]
    path.write_text("".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- job logs


def test_a_log_loses_only_its_entries_older_than_the_cutoff_and_keeps_its_inode(tmp_path):
    log = tmp_path / "news.log"
    _log(log, [60, 45, 31, 30, 2])
    inode = log.stat().st_ino
    removed = maintenance.trim_file(log, TODAY - dt.timedelta(days=30))
    text = log.read_text(encoding="utf-8")
    assert text.startswith(f"=== {TODAY - dt.timedelta(days=30)} 08:00:00")
    assert "day 31" not in text and "day 60" not in text and "day 2" in text
    assert removed > 0 and log.stat().st_ino == inode    # rewritten in place
    assert maintenance.trim_file(log, TODAY - dt.timedelta(days=30)) == 0


def test_a_dry_run_counts_and_logs_without_dates_or_all_old_are_handled(tmp_path):
    log = tmp_path / "a.log"
    _log(log, [40, 1])
    before = log.read_bytes()
    assert maintenance.trim_file(log, TODAY - dt.timedelta(days=30), dry_run=True) > 0
    assert log.read_bytes() == before
    undated = tmp_path / "b.log"
    undated.write_text("no dates here\nnor here\n", encoding="utf-8")
    assert maintenance.trim_file(undated, TODAY) == 0 and undated.read_text() != ""
    old = tmp_path / "c.log"
    _log(old, [90, 80])
    old.write_text(old.read_text() + f"{TODAY - dt.timedelta(days=70)} 09:00:00 IST skipped "
                   "igs news process: already running\n", encoding="utf-8")
    maintenance.trim_file(old, TODAY - dt.timedelta(days=30))
    assert old.read_text() == ""


@pytest.mark.skipif(os.name == "nt", reason="job locks are Linux-only")
def test_a_running_jobs_log_is_skipped_but_this_jobs_own_log_is_trimmed(tmp_path,
                                                                        monkeypatch):
    import fcntl
    for name in ("news", "daily", "maintenance"):
        _log(tmp_path / f"{name}.log", [60, 1])
    held = []
    for name in ("daily", "maintenance"):     # held as igs-job.sh holds them
        handle = (tmp_path / f"{name}.lock").open("w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(handle)
    monkeypatch.setenv("IGS_JOB", "maintenance")
    rep = maintenance.Report()
    maintenance.trim_logs(None, 30, rep, logs=tmp_path, today=TODAY)
    for handle in held:
        handle.close()
    assert "day 60" not in (tmp_path / "news.log").read_text()
    assert "day 60" not in (tmp_path / "maintenance.log").read_text()
    assert "day 60" in (tmp_path / "daily.log").read_text()
    assert rep.skipped == ["logs/daily.log: its job is running"]
    assert rep.removed["log_files"] == 2


# --------------------------------------------------------------------------- unit files


def test_old_schedule_file_copies_beyond_the_newest_few_go(tmp_path):
    for i in range(5):
        (tmp_path / f"igs-news.timer.backup.2026100{i + 1}120000").write_text("old")
    (tmp_path / "igs-sync.service.backup.20261001120000").write_text("old")
    (tmp_path / "igs-news.timer").write_text("current")
    (tmp_path / "other.timer.backup.20261001120000").write_text("not ours")
    rep = maintenance.Report()
    maintenance.prune_unit_backups(3, rep, directory=tmp_path)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["igs-news.timer", "igs-news.timer.backup.20261003120000",
                    "igs-news.timer.backup.20261004120000",
                    "igs-news.timer.backup.20261005120000",
                    "igs-sync.service.backup.20261001120000",
                    "other.timer.backup.20261001120000"]
    assert rep.removed == {"unit_backups": 2}
    maintenance.prune_unit_backups(0, maintenance.Report(), directory=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "igs-news.timer", "other.timer.backup.20261001120000"]


# --------------------------------------------------------------------------- settings, CLI


def test_the_deployment_script_reads_the_backup_retention(capsys, monkeypatch):
    cfg = load_maintenance()
    assert (cfg.backups_keep, cfg.backup_days, cfg.cleanup_enabled) == (5, 30, True)
    assert maintenance.backup_retention(cfg) == "5 30"
    off = cfg.model_copy(update={"cleanup_enabled": False})
    assert maintenance.backup_retention(off) == "off"
    monkeypatch.setattr("igs.config.load_maintenance", lambda: off)
    assert cli.main(["maintenance", "backup-retention"]) == 0
    assert capsys.readouterr().out.strip() == "off"
    with pytest.raises(ValueError):
        MaintenanceConfig(backups_keep=0)          # the newest backup always stays


def test_the_daily_job_removes_nothing_when_cleanup_is_off(capsys, monkeypatch):
    off = load_maintenance().model_copy(update={"cleanup_enabled": False})
    monkeypatch.setattr("igs.config.load_maintenance", lambda: off)
    monkeypatch.setattr("igs.db.connect", lambda *a, **k: pytest.fail("no database needed"))
    assert cli.main(["maintenance", "cleanup", "--trigger", "scheduled"]) == 0
    assert "Cleanup is off" in capsys.readouterr().out


def test_the_summary_names_each_kind():
    text = maintenance.describe({"log_bytes": 3 * 1024 * 1024, "log_files": 2,
                                 "scan_rows": 12345, "checks": 4, "dq_issues": 9,
                                 "unit_backups": 1})
    assert text == ("Removed 3.0 MB of old entries from 2 job log(s); 12,345 scan rows "
                    "without a call; 4 data checks and 9 data-quality issues; 1 old "
                    "schedule file copies.")
    assert maintenance.describe({}, dry_run=True).startswith("Would remove 0 bytes")


# --------------------------------------------------------------------------- database


def _scan(conn, cid, started: dt.datetime, actions: list[str | None]) -> int:
    scan_id = conn.execute("insert into intraday_scan(started_at,status) "
                           "values(%s,'complete') returning scan_id", (started,)).fetchone()[0]
    for i, action in enumerate(actions):
        result = {"candle_end": started.isoformat()} | ({"action": action} if action else {})
        conn.execute("""insert into intraday_signal(scan_id,company_id,symbol,instrument_key,
            observed_at,result) values(%s,%s,%s,%s,%s,%s)""",
                     (scan_id, cid[i], f"S{i}", f"NSE_EQ|S{i}", started, Jsonb(result)))
    conn.commit()
    return scan_id


@pytest.fixture
def stocks(db_conn):
    from test_intraday import seed_stock
    return [seed_stock(db_conn, f"S{i}", f"INE00000000{i}") for i in range(4)]


@pytest.mark.db
def test_old_scan_rows_without_a_call_go_and_calls_and_scans_stay(db_conn, stocks,
                                                                  monkeypatch):
    monkeypatch.setattr(maintenance, "SCANS_PER_BATCH", 1)      # several batches
    old = [_scan(db_conn, stocks, NOW - dt.timedelta(days=d), ["buy", "wait", None, "sell"])
           for d in (40, 20, 15)]
    recent = _scan(db_conn, stocks, NOW - dt.timedelta(days=13), ["wait", "wait", None, "buy"])
    dry = maintenance.Report(dry_run=True)
    maintenance.prune_scans(db_conn, 14, dry, now=NOW)
    assert dry.removed == {"scan_rows": 6}
    rep = maintenance.Report()
    maintenance.prune_scans(db_conn, 14, rep, now=NOW)
    assert rep.removed == {"scan_rows": 6}
    rows = db_conn.execute("select scan_id, result->>'action' from intraday_signal "
                           "order by scan_id, symbol").fetchall()
    assert rows == [(s, a) for s in old for a in ("buy", "sell")] + [
        (recent, "wait"), (recent, "wait"), (recent, None), (recent, "buy")]
    assert db_conn.execute("select count(*) from intraday_scan").fetchone()[0] == 4
    again = maintenance.Report()
    maintenance.prune_scans(db_conn, 14, again, now=NOW)
    assert again.removed.get("scan_rows", 0) == 0


@pytest.mark.db
def test_old_data_checks_and_their_issues_go_but_a_running_check_stays(db_conn):
    for days, status in ((200, "ok"), (190, "running"), (10, "ok")):
        db_conn.execute("insert into sync_run(trigger,started_at,status) values('timer',%s,%s)",
                        (NOW - dt.timedelta(days=days), status))
    for days in (200, 10):
        db_conn.execute("insert into dq_issue(detected_at,severity,category,message) "
                        "values(%s,'warn','test','x')", (NOW - dt.timedelta(days=days),))
    db_conn.commit()
    rep = maintenance.Report()
    maintenance.prune_checks(db_conn, 180, rep, now=NOW)
    assert rep.removed == {"checks": 1, "dq_issues": 1}
    assert db_conn.execute("select status from sync_run order by started_at").fetchall() == [
        ("running",), ("ok",)]


@pytest.mark.db
@pytest.mark.skipif(os.name == "nt", reason="job locks are Linux-only")
def test_the_sync_log_is_left_alone_while_a_data_check_runs(db_conn, tmp_path, monkeypatch):
    from igs.sync import LOCK_KEY
    monkeypatch.delenv("IGS_JOB", raising=False)
    _log(tmp_path / "sync.log", [60, 1])
    with psycopg.connect(os.environ["IGS_TEST_DATABASE_URL"]) as other:
        other.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
        other.commit()
        rep = maintenance.Report()
        maintenance.trim_logs(db_conn, 30, rep, logs=tmp_path, today=TODAY)
        assert rep.skipped == ["logs/sync.log: a data check is running"]
        other.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
        other.commit()
    rep = maintenance.Report()
    maintenance.trim_logs(db_conn, 30, rep, logs=tmp_path, today=TODAY)
    assert rep.skipped == [] and "day 60" not in (tmp_path / "sync.log").read_text()
    # The cleanup released the check lock: a check can start.
    assert db_conn.execute("select pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]
    db_conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
    db_conn.commit()


@pytest.mark.db
def test_a_cleanup_is_recorded_and_a_failing_part_does_not_stop_the_others(
        db_conn, stocks, tmp_path, monkeypatch):
    _scan(db_conn, stocks, NOW - dt.timedelta(days=30), ["wait", "buy", None, None])
    (tmp_path / "logs").mkdir()
    _log(tmp_path / "logs" / "news.log", [60, 1])
    cfg = load_maintenance()
    dry = maintenance.cleanup(db_conn, cfg, dry_run=True, now=NOW, logs=tmp_path / "logs",
                              units=tmp_path)
    assert dry.run_id is None and dry.removed["scan_rows"] == 3
    assert maintenance.last_run(db_conn) is None
    assert "day 60" in (tmp_path / "logs" / "news.log").read_text()

    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(maintenance, "prune_checks", broken)
    rep = maintenance.cleanup(db_conn, cfg, trigger="scheduled", now=NOW,
                              logs=tmp_path / "logs", units=tmp_path)
    assert rep.removed["scan_rows"] == 3 and rep.removed["log_files"] == 1
    assert rep.skipped == ["data checks: failed (RuntimeError: disk on fire)"]
    last = maintenance.last_run(db_conn)
    assert (last["run_id"], last["trigger"]) == (rep.run_id, "scheduled")
    assert last["removed"] == rep.removed and last["skipped"] == rep.skipped
    with pytest.raises(ValueError):
        maintenance.cleanup(db_conn, cfg, trigger="sometimes")


@pytest.mark.db
def test_the_maintenance_tab_cleans_up_now(db_conn, monkeypatch, tmp_path):
    import streamlit
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))     # no real unit files
    monkeypatch.setattr(maintenance, "LOGS", tmp_path / "logs")
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    app = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
    at = AppTest.from_file(str(app), default_timeout=60).run()
    at.sidebar.radio(key="page").set_value("Settings").run()
    assert not at.exception, at.exception
    tab = next(t for t in at.tabs if t.label == "Maintenance")
    assert any("No cleanup has run yet" in c.value for c in tab.caption)
    next(b for b in tab.button if b.label == "Clean up now").click().run()
    assert not at.exception, at.exception
    assert any(s.value.startswith("Removed ") for s in at.success)
    assert maintenance.last_run(db_conn)["trigger"] == "manual"
