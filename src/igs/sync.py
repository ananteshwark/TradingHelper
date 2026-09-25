"""Checking the exchanges for new files: `igs sync`, and the ingest half of `igs daily`.

A check runs every ingest step once and fetches only what is new (price files for days
not loaded yet, listing pages until the first with nothing new, documents not fetched
before). Every step runs even if an earlier one failed, and every failure is reported.

Only one check runs at a time, whatever started it (the app, the scheduled job, the
daily job, the Check now button): they share a PostgreSQL advisory lock. A check that is
not forced does not start sooner than `min_interval_minutes` after the last one began.
Each check is recorded in sync_run, which is what the UI shows. A check whose process died
(the computer was switched off, the process was killed) stays 'running' there until the next
check marks it interrupted; `check_running` asks the lock itself, so the UI is not fooled.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from igs.config import SyncConfig
from igs.ingest import jobs
from igs.normalize.master_db import rebuild_instrument_master
from igs.timeutil import IST, utc_now

LOCK_KEY = 7215460012          # pg advisory lock held for the whole of a check
REPO_ROOT = Path(__file__).resolve().parents[2]
TRIGGERS = ("manual", "startup", "interval", "timer", "daily")


@dataclass
class SyncReport:
    trigger: str
    sync_id: int | None = None
    steps: list[tuple[str, str, str]] = field(default_factory=list)
    new_rows: int = 0
    skipped: str | None = None

    @property
    def failed(self) -> list[str]:
        return [name for name, status, _ in self.steps if status == "failed"]

    @property
    def status(self) -> str:
        if not self.failed:
            return "ok"
        return "failed" if len(self.failed) == len(self.steps) else "partial"


def price_end(now: dt.datetime, after: dt.time) -> dt.date:
    """The last day whose price files are worth asking for: today once NSE has had time
    to publish them (after `after`, IST), else yesterday."""
    local = now.astimezone(IST)
    return local.date() if local.time() >= after else local.date() - dt.timedelta(days=1)


def _rows(result: object) -> int:
    if isinstance(result, list):
        return sum(getattr(r, "rows", 0) or 0 for r in result)
    return getattr(result, "rows", 0) or 0


def _summary(result: object) -> str:
    if isinstance(result, list):
        rows = sum(getattr(r, "rows", 0) for r in result)
        bad = sum(1 for r in result if getattr(r, "http_status", 200) not in (200, 404))
        note = getattr(result[-1], "note", "") if result else ""
        return (f"{len(result)} fetches, {rows} rows" + (f", {bad} not OK" if bad else "")
                + (f" ({note})" if note else ""))
    if hasattr(result, "rows"):
        return f"{result.rows} rows (HTTP {result.http_status})"
    return str(result)[:200] if result is not None else ""


def run_step(rep: SyncReport, ctx: jobs.Context, name: str, fn: Callable[[], object]) -> object:
    try:
        result = fn()
        ctx.conn.commit()
        rep.steps.append((name, "ok", _summary(result)))
        rep.new_rows += _rows(result)
        return result
    except Exception as exc:  # noqa: BLE001 - every failure is reported, none is swallowed
        ctx.conn.rollback()
        rep.steps.append((name, "failed", f"{type(exc).__name__}: {exc}"))
        ctx.dq.emit("error", "sync_step_failed", f"{name}: {exc}",
                    details={"traceback": traceback.format_exc()[-2000:]})
        return None


def ingest_steps(ctx: jobs.Context, rep: SyncReport, day: dt.date, prices_to: dt.date,
                 documents_limit: int | None = None) -> None:
    s = lambda name, fn: run_step(rep, ctx, name, fn)  # noqa: E731
    s("equity list", lambda: jobs.ingest_static(ctx, "nse_equity_list"))
    s("ASM list", lambda: jobs.ingest_static(ctx, "nse_asm"))
    s("GSM list", lambda: jobs.ingest_static(ctx, "nse_gsm"))
    start = (last_price_date(ctx.conn) or prices_to - dt.timedelta(days=7)) \
        + dt.timedelta(days=1)
    s("prices, delivery, index closes",
      lambda: jobs.backfill_prices(ctx, start, prices_to) if start <= prices_to else [])
    s("corporate actions", lambda: jobs.ingest_range(
        ctx, "nse_corporate_actions", day - dt.timedelta(days=30), day + dt.timedelta(days=60),
        chunk_days=90))
    s("announcements", lambda: jobs.ingest_range(
        ctx, "nse_announcements", day - dt.timedelta(days=7), day, chunk_days=30))
    s("insider trades", lambda: jobs.ingest_range(
        ctx, "nse_insider_trading", day - dt.timedelta(days=7), day, chunk_days=30))
    s("results listing", lambda: jobs.ingest_static(ctx, "nse_financial_results_index"))
    s("integrated filing listing", lambda: jobs.ingest_pages(ctx, "nse_integrated_filing_index"))
    s("shareholding listing", lambda: jobs.ingest_static(ctx, "nse_shareholding_index"))
    s("instrument master", lambda: rebuild_instrument_master(ctx.conn, ctx.dq))
    s("results documents",
      lambda: jobs.ingest_documents(ctx, "financial_results", limit=documents_limit))
    s("shareholding documents",
      lambda: jobs.ingest_documents(ctx, "shareholding", limit=documents_limit))


def last_price_date(conn) -> dt.date | None:
    with conn.cursor() as cur:
        cur.execute("select max(trade_date) from price_eod where exchange = 'NSE'")
        return cur.fetchone()[0]


def last_check(conn) -> dict | None:
    """The latest check that ran (running or finished)."""
    with conn.cursor() as cur:
        cur.execute("""select sync_id, trigger, started_at, finished_at, status, new_rows, steps
                       from sync_run order by started_at desc limit 1""")
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("sync_id", "trigger", "started_at", "finished_at", "status", "new_rows", "steps")
    return dict(zip(keys, row, strict=True))


def check_running(conn) -> bool:
    """Whether a process holds the check lock in this database now."""
    with conn.cursor() as cur:
        cur.execute("""select exists(select 1 from pg_locks where locktype = 'advisory'
                         and granted and objsubid = 1
                         and database = (select oid from pg_database
                                         where datname = current_database())
                         and ((classid::bigint << 32) | objid::bigint) = %s)""", (LOCK_KEY,))
        return cur.fetchone()[0]


def run_sync(ctx: jobs.Context, trigger: str, cfg: SyncConfig, *,
             now: dt.datetime | None = None, force: bool = False, wait: bool = False,
             day: dt.date | None = None, prices_to: dt.date | None = None) -> SyncReport:
    """One check. `force` skips the minimum interval; `wait` waits for a running check
    instead of skipping (the daily job uses both)."""
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    now = now or utc_now()
    rep = SyncReport(trigger)
    conn = ctx.conn
    with conn.cursor() as cur:
        if wait:
            cur.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
        else:
            cur.execute("select pg_try_advisory_lock(%s)", (LOCK_KEY,))
            if not cur.fetchone()[0]:
                rep.skipped = "another check is running"
                return rep
    try:
        with conn.cursor() as cur:        # holding the lock: nothing else is running
            cur.execute("update sync_run set status = 'failed', finished_at = %s, note = "
                        "'interrupted: its process ended before the check finished' "
                        "where status = 'running'", (now,))
        conn.commit()
        last = last_check(conn)
        gap = dt.timedelta(minutes=cfg.min_interval_minutes)
        if not force and last is not None and now - last["started_at"] < gap:
            ago = int((now - last["started_at"]).total_seconds() // 60)
            rep.skipped = (f"the last check started {ago} min ago; the minimum interval is "
                           f"{cfg.min_interval_minutes:g} min")
            return rep
        with conn.cursor() as cur:
            cur.execute("insert into sync_run (trigger, started_at) values (%s, %s) "
                        "returning sync_id", (trigger, now))
            rep.sync_id = cur.fetchone()[0]
        conn.commit()
        day = day or now.astimezone(IST).date()
        try:
            ingest_steps(ctx, rep, day, prices_to or price_end(now, cfg.prices_after_ist),
                         cfg.documents_per_check)
            ctx.dq.persist(conn)
        except Exception as exc:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("update sync_run set finished_at = %s, status = 'failed', "
                            "note = %s where sync_id = %s",
                            (utc_now(), f"{type(exc).__name__}: {exc}"[:500], rep.sync_id))
            conn.commit()
            raise
        with conn.cursor() as cur:
            cur.execute("""update sync_run set finished_at = %s, status = %s, new_rows = %s,
                                  steps = %s where sync_id = %s""",
                        (utc_now(), rep.status, rep.new_rows,
                         json.dumps([{"step": n, "status": st, "summary": sm}
                                     for n, st, sm in rep.steps]), rep.sync_id))
        conn.commit()
        return rep
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.commit()


def start_background_sync(trigger: str) -> subprocess.Popen:
    """`igs sync` in its own process (and process group, so Ctrl+C on the app does not cut
    a check short), appending its output to logs/sync.log."""
    logs = REPO_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    log = (logs / "sync.log").open("a", encoding="utf-8")
    extra: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen([sys.executable, "-m", "igs.cli", "sync", "--trigger", trigger],
                            cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, **extra)
