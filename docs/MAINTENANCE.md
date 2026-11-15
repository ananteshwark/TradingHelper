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

**Full backups** of the app, to download and restore, are on the same tab
([BACKUP.md](BACKUP.md)); the cleanup keeps the newest three (`app_backups_keep`), and
starts again any scheduled jobs a restore paused and, killed before its end, never
restarted.

**What is never removed:** records. That means calls, trades, scores, paper tracks and
everything loaded from the downloads. The downloads the database is built from stay too,
unless you switch on their deletion (below); then `igs raw restore` downloads them again
for a rebuild.

## What it removes

| Setting | Default | What goes |
|---|---|---|
| `log_days` | 30 | Job log entries (`logs/*.log`) older than this many days. |
| `scan_days` | 14 | Each intraday scan's rows for stocks it checked without making a call. |
| `check_days` | 180 | The data checks' history (`sync_run`) and the data-quality issues they logged (`dq_issue`). |
| `backups_keep`, `backup_days` | 5, 30 | Deployment backups that are both beyond the newest 5 and older than 30 days. |
| `unit_backups_keep` | 3 | Old copies of the schedule files, beyond the newest 3 of each. |
| `download_days` | 30 | NSE and Screener downloads nothing reads again: verification samples, failed responses and replaced Screener exports. |
| `delete_loaded_nse_files`, `loaded_nse_file_days` | off, 365 | With the switch on, NSE's price files and filing documents once loaded into the database. |

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
- **The NSE files the database is built from stay,** unless the switch below is on:
  price files, filings, listings, lists. `igs rebuild` replays them. (A filing that failed
  to load is also retried, by `igs documents replay`.)
- **Fetching again works:** the same bytes downloaded again land again.

**NSE files already loaded: the switch.**
- With `delete_loaded_nse_files` on (off by default), the cleanup also deletes NSE's files
  once they're loaded into the database and older than `loaded_nse_file_days` (365):
  - the price files: bhavcopies, delivery, index closes and market caps;
  - the filing documents: results, shareholding and insider-trading XBRL.
- After they're loaded, the app runs from the database. Only a rebuild (`igs rebuild`)
  reads them again.
- **Only files NSE keeps at a fixed address are deleted,** and only once loaded:
  - a price file once its prices, delivery or index closes are in the database;
  - a filing once it has loaded. One that failed stays, for its retry.
- **Lists and API answers are never deleted:** the equity list, announcements, corporate
  actions, insider trades, surveillance lists and the rest. NSE changes them, so they
  can't be downloaded again as they were.

**Downloading them again, for a rebuild.**
- **First, the rebuild checks.** Without the files it stops before it changes anything,
  and says how many are missing.

```bash
uv run igs raw restore --dry-run    # how many were deleted, by source
uv run igs rebuild --download       # download them again, then rebuild
uv run igs raw restore              # only download them again
```

- **Each file is checked byte for byte.** It is downloaded again from the address in its
  fetch record, and kept only if its bytes are exactly the first download's (the same
  sha256). So a rebuild gives the same database.
- **If NSE serves a file differently now, or no longer serves it,** it is reported and not
  used. `igs rebuild --download` then doesn't rebuild: the database would lack that
  file's data.
  - The different file is kept as a download of its own, so the difference can be looked
    at.
  - Whether NSE's old archive files stay byte-identical over the years can't be checked
    in advance; the check is what guards against it.
- **The time it takes:** NSE is asked at most once every 5 seconds. A year of price files
  is about 1,250 files, under two hours; each 1,000 filings adds about 1.4 hours.
- **The record of it:** each download made again is landed like any other, with the note
  "downloaded again for …". It is never replayed itself.
- **While the switch is on,** the next daily cleanup deletes the files again. To keep
  them, switch it off first.

**Schedule file copies.** `scripts/install-schedules.sh` keeps a copy of each unit it
replaces, in `~/.config/systemd/user`.

## When it runs

- **Daily:** `igs-maintenance.timer`, at 03:17 IST. Install it with
  `scripts/install-schedules.sh` (once, after the deployment that brings it). Settings >
  Schedules runs it now too.
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

The Maintenance page shows available, used and total space on the server's root
filesystem, and separate lists of its three largest readable files and folders.
Sizes use allocated disk space; nested folder totals overlap. The scan does not follow
symlinks or cross into other filesystems. It runs with the app user's permissions,
so protected paths (including PostgreSQL data and root-owned backups) may be absent.
Unreadable entries or the 30-second scan deadline produce an explicit partial-results
warning. Rankings are cached for five minutes; **Refresh storage** rescans immediately.
Disk capacity is checked on every page refresh. This display never deletes files.
