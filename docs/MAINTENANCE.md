# Cleanup of stale data and backups

Some of what the app keeps is only working material. It grows every day, and nothing
reads it after a few days:
- the job logs;
- the intraday scanner's reading of every stock it checks, every five minutes;
- the history of the 30-minute data checks;
- the backups made before each deployment;
- NSE and Screener downloads that nothing reads again.

The cleanup removes it on a schedule. Each kind has its own setting on the Settings page
(**Maintenance**), from `config/maintenance.yaml`.

**What is never removed:** records. That means calls, trades, scores, paper tracks,
everything loaded from the downloads, and every download the database is built from.

## What it removes

| Setting | Default | What goes |
|---|---|---|
| `log_days` | 30 | Job log entries (`logs/*.log`) older than this many days. |
| `scan_days` | 14 | Each intraday scan's rows for stocks it checked without making a call. |
| `check_days` | 180 | The data checks' history (`sync_run`) and the data-quality issues they logged (`dq_issue`). |
| `backups_keep`, `backup_days` | 5, 30 | Deployment backups that are both beyond the newest 5 and older than 30 days. |
| `unit_backups_keep` | 3 | Old copies of the schedule files, beyond the newest 3 of each. |
| `download_days` | 30 | NSE and Screener downloads nothing reads again: verification samples, failed responses and replaced Screener exports. |

**Job logs.**
- An entry is dated by the line `scripts/igs-job.sh` starts it with, e.g.
  `=== 2026-11-02 03:17:00 IST igs news process`. The checks the app starts itself now
  write the same line.
- A log is trimmed in place, so a process still appending to it keeps writing to it.
- It is trimmed only while its job isn't running: the cleanup takes the job's lock in
  `logs/`, which `igs-job.sh` holds while the job runs.
- `logs/sync.log` is also left alone while a data check runs, including one started from
  the app.
- A log with no dated lines is left alone.
- On Windows, logs aren't trimmed (no job locks there).

**Intraday scans.**
- The scanner keeps a row for every stock it checks, every five minutes: about 15,000
  rows a day.
- Only buy and sell calls are used after the day:
  - their paper record is built from them;
  - the open-calls list uses only the same day's rows.
- So rows without a call go after `scan_days`, counted from midnight IST.
- The calls stay, and so does each scan's own row (`intraday_scan`).

**Data checks.**
- Their history is a check every 30 minutes, with each step's summary.
- The data-quality issues they logged are warnings about single files or documents.
  Nothing reads them after the run: each score run keeps its own summary of them.
- A check still running is never removed.

**Deployment backups.**
- `scripts/deploy-from-github.sh` makes one before each deployment, in
  `/home/anant/deploy-backups`: a database dump and the previous source.
- They belong to root, so the app can't remove them. The deployment script does instead,
  after a successful deployment.
- It reads the two settings with `igs maintenance backup-retention` and removes a backup
  only if it is:
  - older than `backup_days`;
  - not among the newest `backups_keep`.
- The deployment's own backup is never removed.
- **Nothing is removed** after a failed deployment, when the cleanup is off, or when the
  setting can't be read. A problem pruning never fails a deployment.

**Downloads.** Every download is kept in the raw store (`data/raw`): its bytes, and a
fetch record saying what was fetched, when, and the answer. The cleanup removes the bytes,
never the record, of three kinds that nothing reads again, older than `download_days`:
- **Verification samples:** the weekly source check (`igs sources verify`) fetches one from
  each source. A sample is never loaded or replayed; the check's result is kept
  separately.
- **Failed responses:** error pages, files that weren't there (a holiday's price file) and
  retried attempts. Only a successful download is loaded or replayed.
- **Replaced Screener exports:** an export each of whose companies has a newer export.
  Each export holds the company's whole history, its figures stay in the database, and
  an export is never read again after it is loaded. An export with a row for no company
  is kept.

Rules that keep it safe:
- **Shared files:** identical downloads share one file, so a file goes only if no other
  download needs it.
- **No overlap with a data check:** it runs only while no data check runs, so no fetch can
  land the same bytes meanwhile.
- **The NSE files the database is built from all stay:** price files, filings, listings,
  lists. `igs rebuild` replays them, and filings are re-read after a parser change.
- **Fetching again works:** the same bytes downloaded again land again.

**Schedule file copies.** `scripts/install-schedules.sh` keeps a copy of each unit it
replaces, in `~/.config/systemd/user`.

## When it runs

- **Daily:** `igs-maintenance.timer`, at 03:17 IST. Install it with
  `scripts/install-schedules.sh` (once, after the deployment that brings it).
- **Switched off:** with `cleanup_enabled: false`, neither the daily job nor the
  deployment script removes anything.
- **Now:** Settings > Maintenance has **Preview the cleanup**, which counts only, and
  **Clean up now**. Clean up now works even when the daily cleanup is off. From the
  command line:

```bash
uv run igs maintenance cleanup --dry-run     # what it would remove
uv run igs maintenance cleanup               # remove it now
```

Each cleanup is recorded in `maintenance_run` (migration 081), with what it removed and
what it couldn't clean and why. The Maintenance tab shows the last one.

## Disk space

PostgreSQL reuses the space of removed rows for new ones, so the database stops growing.
It doesn't give that space back to the disk.

To shrink the scan table once after the first cleanup:

```sql
vacuum full intraday_signal;
```

This locks the table while it runs, so do it outside market hours.
