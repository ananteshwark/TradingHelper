"""Checking NSE for new files (`igs sync`): when prices are asked for, one check at a time,
the minimum interval, what is recorded, the CLI and the app's sidebar."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import psycopg
import pytest

from igs import sync
from igs.config import load_sources, load_sync
from igs.ingest.jobs import Context
from igs.ingest.raw_store import RawStore
from igs.timeutil import IST

APP = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
NOW = dt.datetime(2026, 9, 25, 20, 0, tzinfo=IST)


def test_todays_prices_are_asked_for_only_after_publication():
    cfg = load_sync()
    assert sync.price_end(NOW, cfg.prices_after_ist) == dt.date(2026, 9, 25)
    assert sync.price_end(NOW.replace(hour=14), cfg.prices_after_ist) == dt.date(2026, 9, 24)
    # A UTC timestamp is read in IST: 13:00 UTC is 18:30 IST, before publication.
    utc = dt.datetime(2026, 9, 25, 13, 0, tzinfo=dt.UTC)
    assert sync.price_end(utc, cfg.prices_after_ist) == dt.date(2026, 9, 24)


def _ctx(conn, tmp_path) -> Context:
    return Context(conn=conn, store=RawStore(tmp_path / "raw"), sources=load_sources())


def _fake_steps(calls: list):
    def steps(ctx, rep, day, prices_to, documents_limit=None):
        calls.append((day, prices_to, documents_limit))
        sync.run_step(rep, ctx, "prices, delivery, index closes", lambda: _Rows(12))
        sync.run_step(rep, ctx, "announcements", lambda: (_ for _ in ()).throw(
            RuntimeError("refused")))
    return steps


class _Rows:
    def __init__(self, n: int) -> None:
        self.rows, self.http_status = n, 200


@pytest.mark.db
def test_a_check_is_recorded_and_the_next_waits_for_the_interval(db_conn, tmp_path,
                                                                 monkeypatch):
    calls: list = []
    monkeypatch.setattr(sync, "ingest_steps", _fake_steps(calls))
    cfg = load_sync()
    rep = sync.run_sync(_ctx(db_conn, tmp_path), "startup", cfg, now=NOW)
    assert rep.status == "partial" and rep.new_rows == 12 and rep.failed == ["announcements"]
    assert calls == [(dt.date(2026, 9, 25), dt.date(2026, 9, 25), cfg.documents_per_check)]
    last = sync.last_check(db_conn)
    assert (last["trigger"], last["status"], last["new_rows"]) == ("startup", "partial", 12)
    assert [s["status"] for s in last["steps"]] == ["ok", "failed"]
    assert "RuntimeError: refused" in last["steps"][1]["summary"]
    # 30 minutes later: skipped, nothing fetched, nothing recorded.
    later = sync.run_sync(_ctx(db_conn, tmp_path), "interval", cfg,
                          now=NOW + dt.timedelta(minutes=30))
    assert later.skipped and "minimum interval" in later.skipped and len(calls) == 1
    assert sync.last_check(db_conn)["sync_id"] == rep.sync_id
    # A forced check (the daily job) runs anyway; so does one after the interval.
    assert sync.run_sync(_ctx(db_conn, tmp_path), "daily", cfg, now=NOW + dt.timedelta(
        minutes=31), force=True).sync_id is not None
    assert sync.run_sync(_ctx(db_conn, tmp_path), "interval", cfg, now=NOW + dt.timedelta(
        hours=2)).sync_id is not None
    assert len(calls) == 3


@pytest.mark.db
def test_one_check_at_a_time(db_conn, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sync, "ingest_steps", _fake_steps(calls))
    other = psycopg.connect(os.environ["IGS_TEST_DATABASE_URL"])
    try:
        with other.cursor() as cur:
            cur.execute("select pg_advisory_lock(%s)", (sync.LOCK_KEY,))
        rep = sync.run_sync(_ctx(db_conn, tmp_path), "timer", load_sync(), now=NOW)
        assert rep.skipped == "another check is running" and not calls
    finally:
        other.close()                                   # releases the lock
    assert sync.run_sync(_ctx(db_conn, tmp_path), "timer", load_sync(), now=NOW).sync_id


@pytest.mark.db
def test_a_check_that_breaks_is_recorded_as_failed(db_conn, tmp_path, monkeypatch):
    def broken(ctx, rep, day, prices_to, documents_limit=None):
        raise RuntimeError("disk full")
    monkeypatch.setattr(sync, "ingest_steps", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        sync.run_sync(_ctx(db_conn, tmp_path), "manual", load_sync(), now=NOW)
    last = sync.last_check(db_conn)
    assert last["status"] == "failed" and last["finished_at"] is not None
    # The lock was released: the next check is not refused as "running".
    monkeypatch.setattr(sync, "ingest_steps", _fake_steps([]))
    assert sync.run_sync(_ctx(db_conn, tmp_path), "manual", load_sync(),
                         now=NOW + dt.timedelta(hours=2)).sync_id


@pytest.mark.db
def test_a_check_whose_process_died_is_marked_interrupted(db_conn, tmp_path, monkeypatch):
    """A row left 'running' by a killed process must not look like a running check."""
    with db_conn.cursor() as cur:
        cur.execute("insert into sync_run (trigger, started_at) values ('startup', %s)",
                    (NOW - dt.timedelta(hours=3),))
    db_conn.commit()
    assert sync.last_check(db_conn)["status"] == "running"
    assert not sync.check_running(db_conn)                  # nobody holds the lock
    other = psycopg.connect(os.environ["IGS_TEST_DATABASE_URL"])
    try:
        with other.cursor() as cur:
            cur.execute("select pg_advisory_lock(%s)", (sync.LOCK_KEY,))
        assert sync.check_running(db_conn)
    finally:
        other.close()
    assert not sync.check_running(db_conn)
    monkeypatch.setattr(sync, "ingest_steps", _fake_steps([]))
    rep = sync.run_sync(_ctx(db_conn, tmp_path), "interval", load_sync(), now=NOW)
    with db_conn.cursor() as cur:
        cur.execute("select status, note from sync_run where sync_id <> %s", (rep.sync_id,))
        status, note = cur.fetchone()
    assert status == "failed" and note.startswith("interrupted")


@pytest.mark.db
def test_cli_sync_with_nothing_verified(db_conn, tmp_path, monkeypatch, capsys):
    """Every download step refuses an unverified source; the steps that need no download
    still run, so the check is partial (exit 0: a scheduler should not treat one refused
    source as a broken job) and says what failed."""
    from igs import cli
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    monkeypatch.setenv("IGS_RAW_ROOT", str(tmp_path / "raw"))
    assert cli.main(["sync", "--trigger", "timer"]) == 0
    out = capsys.readouterr().out
    assert "SourceNotVerified" in out and "(timer): partial" in out
    assert "OK     instrument master" in out
    assert sync.last_check(db_conn)["trigger"] == "timer"
    assert cli.main(["sync"]) == 0                              # within the interval
    assert "skipped" in capsys.readouterr().out


@pytest.mark.db
def test_sidebar_shows_the_last_check(db_conn, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest
    with db_conn.cursor() as cur:
        cur.execute("""insert into sync_run (trigger, started_at, finished_at, status,
                                             new_rows, steps)
                       values ('interval', now(), now(), 'partial', 42,
                               '[{"step": "announcements", "status": "failed",
                                  "summary": "x"}]')""")
    db_conn.commit()
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    text = " ".join(c.value for c in at.sidebar.caption)
    assert "NSE last checked" in text and "42 new rows" in text and "announcements" in text
    # Just checked: the button waits for the minimum interval.
    assert at.sidebar.button(key="sync_now").disabled
    # A check left 'running' by a process that died is shown as such, not as running.
    with db_conn.cursor() as cur:
        cur.execute("insert into sync_run (trigger, started_at) "
                    "values ('startup', now() - interval '3 hours')")
        cur.execute("update sync_run set started_at = now() - interval '4 hours' "
                    "where trigger = 'interval'")
    db_conn.commit()
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    text = " ".join(c.value for c in at.sidebar.caption)
    assert "did not finish" in text and "Checking NSE" not in text
    assert not at.sidebar.button(key="sync_now").disabled
