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
                                 "unit_backups": 1, "downloads": 7,
                                 "download_bytes": 5 * 1024 ** 3})
    assert text == ("Removed 3.0 MB of old entries from 2 job log(s); 12,345 scan rows "
                    "without a call; 4 data checks and 9 data-quality issues; 1 old "
                    "schedule file copies; 7 old downloads nothing reads again (5.0 GB).")
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
                              units=tmp_path, raw=tmp_path / "raw")
    assert dry.run_id is None and dry.removed["scan_rows"] == 3
    assert maintenance.last_run(db_conn) is None
    assert "day 60" in (tmp_path / "logs" / "news.log").read_text()

    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(maintenance, "prune_checks", broken)
    rep = maintenance.cleanup(db_conn, cfg, trigger="scheduled", now=NOW,
                              logs=tmp_path / "logs", units=tmp_path, raw=tmp_path / "raw")
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
    monkeypatch.setenv("IGS_RAW_ROOT", str(tmp_path / "raw"))  # nor real downloads
    monkeypatch.setattr(maintenance, "LOGS", tmp_path / "logs")
    real = streamlit.get_option
    monkeypatch.setattr(streamlit, "get_option", lambda k: "127.0.0.1"
                        if k == "server.address" else real(k))
    app = Path(__file__).resolve().parents[1] / "src" / "igs" / "ui" / "app.py"
    at = AppTest.from_file(str(app), default_timeout=60).run()
    from igs.ui import storage
    monkeypatch.setattr(storage, "_inventory", lambda: ({
        "files": [(1024, "/example/file")], "folders": [(1024, "/example")],
        "skipped": 0, "timed_out": False}, NOW))
    at.sidebar.radio(key="page").set_value("Settings").run()
    assert not any(m.label == "Available" for m in at.metric)
    at.session_state["settings_tab"] = "Maintenance"
    at.run()
    assert not at.exception, at.exception
    assert any(m.label == "Available" for m in at.metric)
    tab = next(t for t in at.tabs if t.label == "Maintenance")
    assert any("No cleanup has run yet" in c.value for c in tab.caption)
    next(b for b in tab.button if b.label == "Clean up now").click().run()
    assert not at.exception, at.exception
    assert any(s.value.startswith("Removed ") for s in at.success)
    assert maintenance.last_run(db_conn)["trigger"] == "manual"


# --------------------------------------------------------------------------- downloads


def _put(store, conn, source, content: bytes, days: int, *, status=200, note="",
         index=True, origin="http"):
    from igs.ingest.raw_store import RawStore
    rec = store.put(source_id=source, content=content, url=None if origin == "manual" else
                    f"https://example.test/{source}", http_status=status, note=note,
                    origin=origin, fetched_at=NOW - dt.timedelta(days=days))
    if index:
        RawStore.index_record(conn, rec)
        conn.commit()
    return rec


def _screener(store, conn, content: bytes, days: int, companies: list[int | None]):
    rec = _put(store, conn, "screener_export", content, days, status=None, origin="manual")
    for cid in companies:
        conn.execute("insert into screener_enrichment(source_fetch_id,company_id,field) "
                     "values(%s,%s,'Sales')", (rec.fetch_id, cid))
    conn.commit()
    return rec


@pytest.mark.db
def test_only_downloads_nothing_reads_again_go_and_a_shared_file_stays(db_conn, stocks,
                                                                       tmp_path):
    from igs.ingest.raw_store import RawStore, RawStoreError
    from igs.ingest.verify import PROBE_NOTE
    store = RawStore(tmp_path / "raw")
    a, b = stocks[:2]
    live = _put(store, db_conn, "nse_equity_list", b"SYMBOL,NAME\nX,Y\n", 40)
    # A verification sample with the same bytes as a live download shares its file: kept.
    shared = _put(store, db_conn, "nse_equity_list", b"SYMBOL,NAME\nX,Y\n", 39,
                  note=PROBE_NOTE, index=False)
    probe = _put(store, db_conn, "nse_cm_bhavcopy_udiff", b"probe zip" * 100, 40,
                 note=PROBE_NOTE, index=False)
    young_probe = _put(store, db_conn, "nse_asm", b"recent sample", 5, note=PROBE_NOTE,
                       index=False)
    missing = _put(store, db_conn, "nse_cm_bhavcopy_udiff", b"<html>404</html>", 40,
                   status=404)
    retried = _put(store, db_conn, "nse_announcements", b"rate limited", 40, status=503,
                   note="attempt 1, retried", index=False)
    old_a = _screener(store, db_conn, b"export A 1", 60, [a])
    new_a = _screener(store, db_conn, b"export A 2", 35, [a])
    both = _screener(store, db_conn, b"screen of A and B", 50, [a, b])    # B not replaced
    unmapped = _screener(store, db_conn, b"screen with an unknown row", 50, [None])
    gone = [probe, missing, retried, old_a]
    kept = [live, shared, young_probe, new_a, both, unmapped]

    dry = maintenance.Report(dry_run=True)
    maintenance.prune_downloads(db_conn, 30, dry, root=store.root, now=NOW)
    assert dry.removed["downloads"] == 4
    assert all((store.root / r.blob_path).exists() for r in gone)
    rep = maintenance.Report()
    maintenance.prune_downloads(db_conn, 30, rep, root=store.root, now=NOW)
    assert rep.removed == {"downloads": 4,
                           "download_bytes": sum(r.size_bytes for r in gone)}
    assert not any((store.root / r.blob_path).exists() for r in gone)
    assert all(store.read_bytes(r) for r in kept)
    # The fetch records stay: what was fetched, and the answer, are still known.
    assert all(store.get(r.fetch_id).http_status == r.http_status for r in gone)
    with pytest.raises(RawStoreError, match="cleanup"):
        store.read_bytes(old_a)
    again = maintenance.Report()
    maintenance.prune_downloads(db_conn, 30, again, root=store.root, now=NOW)
    assert again.removed == {}
    # The same bytes fetched again land again.
    back = _put(store, db_conn, "nse_cm_bhavcopy_udiff", b"<html>404</html>", 1, status=404)
    assert store.read_bytes(back) == b"<html>404</html>"


@pytest.mark.db
def test_downloads_wait_while_a_data_check_runs(db_conn, tmp_path):
    from igs.ingest.raw_store import RawStore
    from igs.ingest.verify import PROBE_NOTE
    from igs.sync import LOCK_KEY
    store = RawStore(tmp_path / "raw")
    probe = _put(store, db_conn, "nse_asm", b"old sample", 40, note=PROBE_NOTE, index=False)
    with psycopg.connect(os.environ["IGS_TEST_DATABASE_URL"]) as other:
        other.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
        other.commit()
        rep = maintenance.Report()
        maintenance.prune_downloads(db_conn, 30, rep, root=store.root, now=NOW)
        other.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
        other.commit()
    assert rep.skipped == ["downloads: a data check is running"]
    assert (store.root / probe.blob_path).exists()


# --------------------------------------------------------------------------- NSE files


def _prices(conn) -> list[tuple]:
    return conn.execute("select trade_date, isin, series, close::float8, delivery_qty "
                        "from price_eod order by 1, 2, 3").fetchall()


@pytest.mark.db
def test_loaded_nse_files_go_only_with_the_switch_and_come_back_for_a_rebuild(
        db_conn, tmp_path, capsys):
    import httpx
    from test_pipeline import SOURCES, _ingest_everything, _routes, _transport, _verify_all

    from igs.ingest import jobs, restore
    from igs.ingest.http import Fetcher
    from igs.ingest.raw_store import RESTORE_NOTE, RawStore, RawStoreError
    from igs.timeutil import utc_now
    store = RawStore(tmp_path / "raw")
    routes = _routes()
    fetcher = Fetcher(store, min_interval_s=0, host_min_interval_s={},
                      client=httpx.Client(transport=_transport(routes)))
    ctx = jobs.Context(conn=db_conn, store=store, sources=SOURCES, fetcher=fetcher)
    _verify_all(ctx)
    _ingest_everything(ctx)
    prices = _prices(db_conn)
    assert prices
    later = utc_now() + dt.timedelta(days=800)              # every download is old by then
    cfg = load_maintenance()
    assert not cfg.delete_loaded_nse_files                  # off unless switched on
    off = maintenance.cleanup(db_conn, cfg, now=later, logs=tmp_path / "logs",
                              units=tmp_path, raw=store.root)
    assert "nse_files" not in off.removed and not restore.missing(store)

    on = cfg.model_copy(update={"delete_loaded_nse_files": True})
    young = maintenance.cleanup(db_conn, on, now=utc_now() + dt.timedelta(days=100),
                                logs=tmp_path / "logs", units=tmp_path, raw=store.root)
    assert "nse_files" not in young.removed                 # loaded, but not 365 days old
    rep = maintenance.cleanup(db_conn, on, now=later, logs=tmp_path / "logs",
                              units=tmp_path, raw=store.root)
    assert rep.removed["nse_files"] > 0 and rep.removed["nse_file_bytes"] > 0
    assert "igs raw restore" in maintenance.describe(rep.removed)
    gone = restore.missing(store)
    # Only NSE's files at a fixed address went; the lists and API answers stayed.
    assert gone and {r.source_id for r in gone} <= set(maintenance.LOADED_NSE_FILES)
    assert {"nse_cm_bhavcopy_udiff", "nse_sec_bhavdata_full", "nse_mto_delivery",
            "nse_index_close_all"} <= {r.source_id for r in gone}
    for rec in store.iter_records("nse_equity_list"):
        assert store.read_bytes(rec)
    # The app runs on from the database; a rebuild stops before it changes anything.
    with pytest.raises(RawStoreError, match="igs rebuild --download"):
        jobs.rebuild_from_raw(ctx)
    assert _prices(db_conn) == prices

    # NSE now serves one file differently: it is reported, kept apart, and not used.
    first = gone[0]
    changed = dict(routes, **{first.url: routes[first.url] + b"\n"})
    other = Fetcher(store, min_interval_s=0, host_min_interval_s={},
                    client=httpx.Client(transport=_transport(changed)))
    got = restore.restore(store, other, SOURCES, records=[first])
    assert got.changed == [first.fetch_id] and not got.complete
    assert restore.missing(store)[0].fetch_id == first.fetch_id

    # Served as first downloaded, everything comes back and the rebuild gives the same.
    got = restore.restore(store, fetcher, SOURCES)
    assert got.complete and got.restored == len(gone), restore.describe(got)
    assert not restore.missing(store)
    again = [r for r in store.iter_records("nse_cm_bhavcopy_udiff")
             if r.note.startswith(RESTORE_NOTE)]
    assert again and not any(jobs.replayable(r) for r in again)   # never replayed twice
    walks: dict[str, int] = {}
    real = store.iter_records

    def counted(source_id=None):
        walks[source_id] = walks.get(source_id, 0) + 1
        return real(source_id)
    store.iter_records = counted
    jobs.rebuild_from_raw(ctx)
    assert _prices(db_conn) == prices
    assert walks and max(walks.values()) == 1        # one walk of the raw store, not two


@pytest.mark.db
def test_a_loaded_filing_document_goes_and_one_that_failed_stays(db_conn, tmp_path):
    from igs.ingest.raw_store import RawStore
    store = RawStore(tmp_path / "raw")
    loaded = _put(store, db_conn, "nse_xbrl_document", b"<xbrl>loaded</xbrl>", 400)
    failed = _put(store, db_conn, "nse_xbrl_document", b"<xbrl>failed</xbrl>", 400)
    for rec, status in ((loaded, "loaded"), (failed, "failed")):
        db_conn.execute("insert into document_processing(fetch_id,status,parser_version) "
                        "values(%s,%s,'v')", (rec.fetch_id, status))
    db_conn.commit()
    assert maintenance.loaded_nse_files(db_conn, NOW - dt.timedelta(days=365)) == {
        loaded.fetch_id}
    rep = maintenance.Report()
    maintenance.prune_downloads(db_conn, 30, rep, root=store.root, now=NOW, loaded_days=365)
    assert rep.removed == {"nse_files": 1, "nse_file_bytes": loaded.size_bytes}
    assert store.read_bytes(failed)            # a failed one is read again by its replay


def test_the_switch_needs_at_least_30_days(monkeypatch):
    with pytest.raises(ValueError):
        MaintenanceConfig(loaded_nse_file_days=7)


def test_only_nse_addresses_are_asked_again(tmp_path):
    from igs.ingest import restore
    from igs.ingest.raw_store import RawStore
    store = RawStore(tmp_path / "raw")
    recs = [store.put(source_id="nse_cm_bhavcopy_udiff", content=c, url=u, http_status=200,
                      fetched_at=NOW) for c, u in ((b"a", "https://example.test/x.zip"),
                                                   (b"b", "http://nsearchives.nseindia.com/x"),
                                                   (b"c", "https://nseindia.com.evil.test/x"))]
    for rec in recs:
        (store.root / rec.blob_path).unlink()
    asked = []

    class Fetcher:
        def get(self, *args, **kwargs):
            asked.append(args)

    got = restore.restore(store, Fetcher(), records=recs)
    assert not asked and len(got.failed) == 3
    assert all("not an NSE address" in f for f in got.failed)
    assert restore._nse_address("https://nsearchives.nseindia.com/content/x.zip")


@pytest.mark.db
def test_rebuild_download_stops_before_changing_anything_if_a_file_cant_come_back(
        db_conn, monkeypatch, tmp_path, capsys):
    from igs.ingest import jobs, restore
    monkeypatch.setenv("IGS_DATABASE_URL", os.environ["IGS_TEST_DATABASE_URL"])
    monkeypatch.setenv("IGS_RAW_ROOT", str(tmp_path / "raw"))
    monkeypatch.setattr(restore, "restore", lambda *a, **k: restore.Restored(
        restored=2, failed=["x: HTTP 404"]))
    monkeypatch.setattr(jobs, "rebuild_from_raw",
                        lambda ctx: pytest.fail("must not rebuild"))
    assert cli.main(["rebuild", "--download"]) == 1
    out = capsys.readouterr()
    assert "could not be downloaded" in out.out and "Not rebuilt" in out.err
    from igs import sync
    assert not sync.check_running(db_conn)               # the check lock was released


def test_the_cleanup_restarts_schedules_a_killed_restore_left_paused(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from igs import backup, schedules
    calls = []
    monkeypatch.setattr(schedules, "_systemctl", lambda *a, **k: calls.append(a) or NS(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(backup, "job_running", lambda: False)
    record = backup.backup_dir() / backup.PAUSED
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("igs-news.timer\n")
    dry = maintenance.Report(dry_run=True)
    maintenance._resume_timers(dry)
    assert not calls and record.exists() and dry.removed == {}
    rep = maintenance.Report()
    maintenance._resume_timers(rep)
    assert calls == [("start", "igs-news.timer")] and not record.exists()
    assert rep.removed == {"timers_resumed": 1}
    assert "started again 1 schedule(s)" in maintenance.describe(rep.removed)


@pytest.mark.skipif(os.name == "nt", reason="the job script is for Linux")
def test_a_momentary_probe_of_the_job_lock_does_not_make_the_job_skip_itself(tmp_path):
    import fcntl
    import shutil
    import subprocess
    import threading
    import time
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "bin").mkdir()
    shutil.copy(root / "scripts" / "igs-job.sh", tmp_path / "scripts" / "igs-job.sh")
    fake = tmp_path / "bin" / "uv"                      # stands in for `uv run igs ...`
    fake.write_text('#!/bin/sh\necho "ran: $*"\n')
    fake.chmod(0o755)
    (tmp_path / "logs").mkdir()
    lock = (tmp_path / "logs" / "demo.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX)                    # as job_running()'s probe does

    def release():
        time.sleep(1)
        fcntl.flock(lock, fcntl.LOCK_UN)
    threading.Thread(target=release).start()
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    subprocess.run(["bash", str(tmp_path / "scripts" / "igs-job.sh"), "demo", "x"], env=env,
                   check=True, timeout=30)
    text = (tmp_path / "logs" / "demo.log").read_text()
    assert "ran: run --all-groups igs demo x" in text and "skipped" not in text
    # A job that is running holds the lock to its end: the second start is skipped.
    fcntl.flock(lock, fcntl.LOCK_EX)
    subprocess.run(["bash", str(tmp_path / "scripts" / "igs-job.sh"), "demo", "y"], env=env,
                   check=True, timeout=30)
    fcntl.flock(lock, fcntl.LOCK_UN)
    assert "skipped igs demo y: already running" in (tmp_path / "logs" / "demo.log").read_text()
