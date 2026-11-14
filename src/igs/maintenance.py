"""Cleanup of stale data and backups (config/maintenance.yaml, docs/MAINTENANCE.md).

Each kind has its own setting:
- **job logs** (logs/*.log): entries older than `log_days`;
- **intraday scans**: each scan's rows for stocks it checked without making a call, older
  than `scan_days`. Buy and sell calls stay: their record (intraday_call_outcome) is built
  from them, and the scan itself (intraday_scan) stays too;
- **the history of data checks** (sync_run) and the data-quality issues they logged
  (dq_issue), older than `check_days`. Each score run keeps its own summary of them;
- **old copies of the schedule files** (scripts/install-schedules.sh keeps one each time it
  replaces a changed unit): all but the newest `unit_backups_keep` of each;
- **NSE and Screener downloads nothing reads again** (in the raw store), older than
  `download_days`: verification samples, failed responses and replaced Screener exports.
  Their fetch records stay;
- **with its switch on** (`delete_loaded_nse_files`, off by default), NSE's price files
  and filing documents once loaded, older than `loaded_nse_file_days`. Only files NSE
  keeps at a fixed address, so `igs raw restore` (or `igs rebuild --download`) can
  download them again for a rebuild, checked byte for byte (igs.ingest.restore).

The deployment backups (a database dump and the source, before each deploy) belong to
root, so the deployment script removes old ones itself after a successful deploy, reading
`backups_keep` and `backup_days` from `igs maintenance backup-retention`.

Records are never removed: calls, trades, scores, paper tracks, what was loaded from the
downloads, and every download the database is built from. A job log is trimmed only
while its job is not running (the job's lock in logs/, which scripts/igs-job.sh holds),
and logs/sync.log only while no data check is running.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.types.json import Jsonb

from igs.config import MaintenanceConfig
from igs.timeutil import IST, utc_now

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGS = REPO_ROOT / "logs"
# The first line of each entry, as scripts/igs-job.sh and start_background_sync write it:
# "=== 2026-11-02 03:17:00 IST igs ..." or "2026-11-02 03:17:00 IST skipped igs ...".
ENTRY = re.compile(rb"^(?:=== )?(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2} ")
UNIT_BACKUP = re.compile(r"^(igs-[\w.@-]+)\.backup\.(\d{14})$")
SCANS_PER_BATCH = 100        # about 20,000 rows at 197 stocks a scan; one commit each
CALLS = "coalesce(result->>'action', '') in ('buy', 'sell')"


@dataclass
class Report:
    dry_run: bool = False
    removed: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    run_id: int | None = None

    def add(self, key: str, n: int) -> None:
        self.removed[key] = self.removed.get(key, 0) + n


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def backup_retention(cfg: MaintenanceConfig) -> str:
    """What the deployment script reads: "KEEP DAYS", or "off" to remove nothing."""
    return f"{cfg.backups_keep} {cfg.backup_days}" if cfg.cleanup_enabled else "off"


# --------------------------------------------------------------------------- job logs


def _cut_at(f, cutoff: dt.date) -> int | None:
    """Where the first entry dated on or after `cutoff` starts: the bytes before it go.
    The whole file when every entry is older; None when no line is dated (left alone)."""
    pos, dated = 0, False
    f.seek(0)
    for line in f:
        m = ENTRY.match(line)
        if m:
            try:
                day = dt.date.fromisoformat(m.group(1).decode())
            except ValueError:
                day = None
            if day is not None:
                if day >= cutoff:
                    return pos
                dated = True
        pos += len(line)
    return pos if dated else None


def trim_file(path: Path, cutoff: dt.date, *, dry_run: bool = False) -> int:
    """Remove every entry dated before `cutoff` from the start of `path`, in place (a
    process appending to it keeps writing to the same file). Returns the bytes removed."""
    with path.open("r+b") as f:
        cut = _cut_at(f, cutoff)
        if not cut or dry_run:
            return cut or 0
        read_at, write_at = cut, 0
        while True:
            f.seek(read_at)
            chunk = f.read(1 << 20)
            if not chunk:
                break
            f.seek(write_at)
            f.write(chunk)
            read_at += len(chunk)
            write_at += len(chunk)
        f.truncate(write_at)
        return cut


@contextmanager
def _job_lock(lock: Path) -> Iterator[bool]:
    """The lock scripts/igs-job.sh holds while the job runs: True if taken (not running)."""
    import fcntl
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _no_check_running(conn) -> Iterator[bool]:
    """The data checks' lock (igs.sync), so no check started from the app writes to
    logs/sync.log while it is trimmed: True if taken."""
    from igs.sync import LOCK_KEY
    got = conn.execute("select pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]
    conn.commit()
    try:
        yield got
    finally:
        if got:
            conn.rollback()
            conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
            conn.commit()


def trim_logs(conn, days: int, rep: Report, *, logs: Path | None = None,
              today: dt.date | None = None) -> None:
    logs = logs or LOGS
    if not logs.is_dir():
        return
    if os.name == "nt":
        rep.skipped.append("job logs: trimmed on Linux only (no job locks on Windows)")
        return
    cutoff = (today or utc_now().astimezone(IST).date()) - dt.timedelta(days=days)
    own = os.environ.get("IGS_JOB")       # set by scripts/igs-job.sh: this job's own log
    for path in sorted(logs.glob("*.log")):
        name = path.stem
        try:
            if name == own:               # its lock is this job's; only it writes there
                removed = trim_file(path, cutoff, dry_run=rep.dry_run)
            else:
                with _job_lock(logs / f"{name}.lock") as free:
                    if not free:
                        rep.skipped.append(f"logs/{path.name}: its job is running")
                        continue
                    if name == "sync":
                        with _no_check_running(conn) as idle:
                            if not idle:
                                rep.skipped.append(f"logs/{path.name}: a data check is "
                                                   "running")
                                continue
                            removed = trim_file(path, cutoff, dry_run=rep.dry_run)
                    else:
                        removed = trim_file(path, cutoff, dry_run=rep.dry_run)
        except OSError as exc:
            rep.skipped.append(f"logs/{path.name}: {type(exc).__name__}")
            continue
        if removed:
            rep.add("log_files", 1)
            rep.add("log_bytes", removed)


# --------------------------------------------------------------------------- database


def _scan_cutoff(now: dt.datetime, days: int) -> dt.datetime:
    """Midnight IST `days` days before today: today's readings always stay (a call is
    withdrawn by a later reading the same day)."""
    day = now.astimezone(IST).date() - dt.timedelta(days=days)
    return dt.datetime.combine(day, dt.time(0), IST)


def prune_scans(conn, days: int, rep: Report, *, now: dt.datetime | None = None) -> None:
    """Each scan's rows for stocks it checked without making a call, from scans started
    before the cutoff; in batches of scans, one commit each."""
    cutoff = _scan_cutoff(now or utc_now(), days)
    upto = conn.execute("""select coalesce(min(scan_id),
                                  (select coalesce(max(scan_id), 0) + 1 from intraday_scan))
                           from intraday_scan where started_at >= %s""", (cutoff,)).fetchone()[0]
    if rep.dry_run:
        n = conn.execute(f"select count(*) from intraday_signal where scan_id < %s "
                         f"and not {CALLS}", (upto,)).fetchone()[0]
        rep.add("scan_rows", n)
        conn.rollback()
        return
    lo = conn.execute(f"select min(scan_id) from intraday_signal where scan_id < %s "
                      f"and not {CALLS}", (upto,)).fetchone()[0]
    while lo is not None and lo < upto:
        hi = min(lo + SCANS_PER_BATCH, upto)
        n = conn.execute(f"delete from intraday_signal where scan_id >= %s and scan_id < %s "
                         f"and not {CALLS}", (lo, hi)).rowcount
        conn.commit()
        rep.add("scan_rows", n)
        lo = hi


def prune_checks(conn, days: int, rep: Report, *, now: dt.datetime | None = None) -> None:
    cutoff = (now or utc_now()) - dt.timedelta(days=days)
    for key, sql in (("checks", "from sync_run where started_at < %s and status <> 'running'"),
                     ("dq_issues", "from dq_issue where detected_at < %s")):
        if rep.dry_run:
            rep.add(key, conn.execute(f"select count(*) {sql}", (cutoff,)).fetchone()[0])
        else:
            rep.add(key, conn.execute(f"delete {sql}", (cutoff,)).rowcount)
    if rep.dry_run:
        conn.rollback()
    else:
        conn.commit()


# --------------------------------------------------------------------------- unit files


def prune_unit_backups(keep: int, rep: Report, *, directory: Path | None = None) -> None:
    """All but the newest `keep` copies of each igs-* unit that install-schedules.sh kept."""
    directory = directory or unit_dir()
    if not directory.is_dir():
        return
    copies: dict[str, list[tuple[str, Path]]] = {}
    for path in directory.iterdir():
        m = UNIT_BACKUP.match(path.name)
        if m and path.is_file():
            copies.setdefault(m.group(1), []).append((m.group(2), path))
    for found in copies.values():
        for _, path in sorted(found, reverse=True)[keep:]:
            if not rep.dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    rep.skipped.append(f"{path.name}: {type(exc).__name__}")
                    continue
            rep.add("unit_backups", 1)


# --------------------------------------------------------------------------- downloads

# Screener exports a newer export of the same company replaced: every company with rows in
# the export has a later one. An export with a row for no company is never replaced.
REPLACED_SCREENER = """
    with export as (
        select distinct e.source_fetch_id, e.company_id, p.fetched_at
        from screener_enrichment e join raw_payload p on p.fetch_id = e.source_fetch_id
        where p.source_id = 'screener_export'),
    flagged as (
        select a.source_fetch_id, a.fetched_at,
               a.company_id is not null and exists (
                   select 1 from export b where b.company_id = a.company_id
                     and b.fetched_at > a.fetched_at) as replaced
        from export a)
    select source_fetch_id from flagged where fetched_at < %s
    group by source_fetch_id having bool_and(replaced)"""


# NSE's files at a fixed address, once loaded: what the switch delete_loaded_nse_files
# removes, and igs.ingest.restore downloads again for a rebuild. Each with the test that it
# was loaded. Lists and API answers (the equity list, announcements, corporate actions...)
# are never removed: they change, so they can't be downloaded again as they were.
_PRICES = "exists (select 1 from price_eod e where e.source_fetch_id = p.fetch_id)"
_DELIVERY = ("exists (select 1 from price_eod e where e.exchange = 'NSE' and "
             "e.trade_date = (p.request_params->>'date')::date and e.delivery_qty is not null)")
LOADED_NSE_FILES = {
    "nse_cm_bhavcopy_udiff": _PRICES,
    "nse_cm_bhavcopy_legacy": _PRICES,
    "nse_sec_bhavdata_full": _DELIVERY,
    "nse_mto_delivery": _DELIVERY,
    "nse_index_close_all":
        "exists (select 1 from index_price e where e.source_fetch_id = p.fetch_id)",
    "nse_market_cap":
        "exists (select 1 from nse_issue_size e where e.source_fetch_id = p.fetch_id)",
    "nse_xbrl_document": "exists (select 1 from document_processing d where "
                         "d.fetch_id = p.fetch_id and d.status = 'loaded')",
    "nse_insider_xbrl":
        "exists (select 1 from insider_trade e where e.source_fetch_id = p.fetch_id)",
}


def loaded_nse_files(conn, cutoff: dt.datetime) -> set[str]:
    """The fetch ids of NSE's files at a fixed address, downloaded before `cutoff` and
    loaded into the database."""
    out: set[str] = set()
    for source_id, loaded in LOADED_NSE_FILES.items():
        out.update(r[0] for r in conn.execute(
            f"""select p.fetch_id from raw_payload p
                where p.source_id = %s and p.http_status = 200 and p.origin = 'http'
                  and p.url is not null and p.fetched_at < %s and {loaded}""",
            (source_id, cutoff)).fetchall())
    return out


def _not_needed(fetch_id: str, fetched_at: dt.datetime, status: int | None, note: str,
                cutoff: dt.datetime, chosen: set[str]) -> bool:
    """A download nothing reads again: a verification sample (never loaded, never
    replayed), a failed response (an error or a missing file: only a 200 is loaded or
    replayed) or a download made again for an earlier one's bytes, older than the cutoff;
    or one of `chosen` (replaced Screener exports, and with the switch on loaded NSE
    files), each picked against its own cutoff."""
    from igs.ingest.raw_store import RESTORE_NOTE
    from igs.ingest.verify import PROBE_NOTE
    return fetch_id in chosen or fetched_at < cutoff and (
        note == PROBE_NOTE or note.startswith(RESTORE_NOTE)
        or (status is not None and status != 200))


def prune_downloads(conn, days: int, rep: Report, *, root: Path | None = None,
                    now: dt.datetime | None = None, loaded_days: int | None = None) -> None:
    """Remove the files of downloads nothing reads again, older than `days`, from the raw
    store; with `loaded_days` (the switch on), also NSE's loaded files older than that. A
    file is shared by every download with the same bytes, so it goes only if no other
    download needs it. The fetch records stay, so what was fetched, when and with what
    answer is still known. Done while no data check runs, so no fetch can land the same
    bytes meanwhile."""
    from igs.ingest.raw_store import FetchRecord
    if root is None:
        from igs.cli import raw_root
        root = raw_root()
    fetches = root / "fetches"
    if not fetches.is_dir():
        return
    now = now or utc_now()
    cutoff = now - dt.timedelta(days=days)
    with _no_check_running(conn) as idle:
        if not idle:
            rep.skipped.append("downloads: a data check is running")
            return
        replaced = {r[0] for r in conn.execute(REPLACED_SCREENER, (cutoff,)).fetchall()}
        loaded = (loaded_nse_files(conn, now - dt.timedelta(days=loaded_days))
                  if loaded_days is not None else set())
        chosen = replaced | loaded
        needed: set[str] = set()
        for fetch_id, fetched_at, status, note, sha in conn.execute(
                "select fetch_id, fetched_at, http_status, note, content_sha256 "
                "from raw_payload").fetchall():
            if not _not_needed(fetch_id, fetched_at, status, note, cutoff, chosen):
                needed.add(sha)
        conn.rollback()
        candidates: dict[str, tuple[str, int, str]] = {}
        for path in fetches.rglob("*.json"):
            try:
                rec = FetchRecord.from_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, KeyError):
                rep.skipped.append(f"downloads: {path.name} can't be read, so nothing was "
                                   "removed")
                return
            if _not_needed(rec.fetch_id, rec.fetched_at, rec.http_status, rec.note, cutoff,
                           chosen):
                kind = ("nse_files" if rec.fetch_id in loaded
                        or candidates.get(rec.content_sha256, ("", 0, ""))[2] == "nse_files"
                        else "downloads")
                candidates[rec.content_sha256] = (rec.blob_path, rec.size_bytes, kind)
            else:
                needed.add(rec.content_sha256)
        for sha, (blob, size, kind) in sorted(candidates.items()):
            path = root / blob
            if (sha in needed or Path(blob).parts[:1] != ("blobs",) or path.name != sha
                    or not path.is_file()):
                continue
            if not rep.dry_run:
                try:
                    if os.name == "nt":            # read-only files can't be removed there
                        path.chmod(0o644)
                    path.unlink()
                except OSError as exc:
                    rep.skipped.append(f"downloads: {sha[:12]}: {type(exc).__name__}")
                    continue
            rep.add(kind, 1)
            rep.add({"downloads": "download_bytes", "nse_files": "nse_file_bytes"}[kind], size)


def _resume_timers(rep: Report) -> None:
    """Timers a restore paused and, killed before its end, never started again."""
    from igs import backup
    if rep.dry_run:
        return
    started = backup.resume_paused_timers(log=lambda s: None)
    if started:
        rep.add("timers_resumed", len(started))


def _prune_backups(keep: int, rep: Report) -> None:
    """Full backups beyond the newest `keep`, while no backup or restore runs."""
    from igs import backup
    if backup.job_running():
        rep.skipped.append("full backups: a backup or restore is running")
        return
    rep.add("app_backups", len(backup.prune(keep, dry_run=rep.dry_run)))


# --------------------------------------------------------------------------- the cleanup


def cleanup(conn, cfg: MaintenanceConfig, *, trigger: str = "manual", dry_run: bool = False,
            now: dt.datetime | None = None, logs: Path | None = None,
            units: Path | None = None, raw: Path | None = None) -> Report:
    """One cleanup of everything above, recorded in maintenance_run (unless a dry run,
    which only counts). Each part runs even if an earlier one failed."""
    if trigger not in ("scheduled", "manual"):
        raise ValueError(f"unknown trigger {trigger!r}")
    now = now or utc_now()
    rep = Report(dry_run=dry_run)
    parts = (("job logs", lambda: trim_logs(conn, cfg.log_days, rep, logs=logs,
                                            today=now.astimezone(IST).date())),
             ("intraday scans", lambda: prune_scans(conn, cfg.scan_days, rep, now=now)),
             ("data checks", lambda: prune_checks(conn, cfg.check_days, rep, now=now)),
             ("schedule file copies", lambda: prune_unit_backups(
                 cfg.unit_backups_keep, rep, directory=units)),
             ("full backups", lambda: _prune_backups(cfg.app_backups_keep, rep)),
             ("paused schedules", lambda: _resume_timers(rep)),
             ("downloads", lambda: prune_downloads(
                 conn, cfg.download_days, rep, root=raw, now=now,
                 loaded_days=cfg.loaded_nse_file_days if cfg.delete_loaded_nse_files
                 else None)))
    for name, part in parts:
        try:
            part()
        except Exception as exc:  # noqa: BLE001 - reported in the run; the rest still run
            conn.rollback()
            rep.skipped.append(f"{name}: failed ({type(exc).__name__}: {exc})"[:300])
    if not dry_run:
        rep.run_id = conn.execute(
            "insert into maintenance_run (trigger, removed, skipped) values (%s, %s, %s) "
            "returning run_id", (trigger, Jsonb(rep.removed), Jsonb(rep.skipped))).fetchone()[0]
        conn.commit()
    return rep


def last_run(conn) -> dict | None:
    row = conn.execute("select run_id, started_at, trigger, removed, skipped "
                       "from maintenance_run order by run_id desc limit 1").fetchone()
    conn.rollback()
    if row is None:
        return None
    return dict(zip(("run_id", "started_at", "trigger", "removed", "skipped"), row,
                    strict=True))


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return ""


def describe(removed: dict, *, dry_run: bool = False) -> str:
    """One line: what was removed (or a dry run would remove)."""
    r = lambda k: removed.get(k, 0)  # noqa: E731
    parts = [f"{_size(r('log_bytes'))} of old entries from {r('log_files')} job log(s)",
             f"{r('scan_rows'):,} scan rows without a call",
             f"{r('checks'):,} data checks and {r('dq_issues'):,} data-quality issues",
             f"{r('unit_backups'):,} old schedule file copies",
             f"{r('downloads'):,} old downloads nothing reads again "
             f"({_size(r('download_bytes'))})"]
    if r("app_backups"):
        parts.append(f"{r('app_backups'):,} old full backups")
    if r("timers_resumed"):
        parts.append(f"started again {r('timers_resumed')} schedule(s) a restore left paused")
    if r("nse_files"):
        parts.append(f"{r('nse_files'):,} NSE files already loaded "
                     f"({_size(r('nse_file_bytes'))}; `igs raw restore` downloads them again)")
    return ("Would remove " if dry_run else "Removed ") + "; ".join(parts) + "."
