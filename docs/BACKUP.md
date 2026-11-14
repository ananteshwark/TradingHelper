# Full backups: download one, and restore one

Settings > Maintenance > **Full backups** makes a backup of the whole app as one file. You
can download it to your computer, and later upload it back and restore it. The command
line does the same with `igs backup create | list | restore`.

## What a backup holds

One gzipped tar file, `igs-backup-<UTC time>.tar.gz`:

| Part | What | Included |
|---|---|---|
| The database | Everything the app has loaded and recorded, as a PostgreSQL dump (`pg_dump --format=custom`) | always |
| Settings and accounts | What the Settings page changed (`data/settings`), with the sign-in accounts | always |
| Other files | The app's other state in `data/`: the look-ahead gate's record (`gates/`) and the backtest status (`backtest/`); and `reports/` | always |
| The secrets | `.env` (API keys, tokens, passwords) and `.streamlit/secrets.toml` | if ticked |
| The downloads | `data/raw`: every file downloaded from NSE and the other sources | if ticked |
| The code | The app's code at that commit (`git archive`), for reference | always |

**Left out:** everything else in `data/`: the backups themselves, caches that are
downloaded again (`intraday/`), fine-tuning and GPU-lab work, the notification spool, and
the local research mirror (`research-pg/`, a live PostgreSQL cluster, and `research-sync/`,
which holds an SSH key); and the logs. Only the parts named above are backed up, so a new
folder in `data/` is never swept in by accident.

**The manifest** lists what the backup holds: when it was made, on which computer, the
app's commit, the database's migrations, and each part's size. It also records the
database dump's sha256.

**Keep the file private.** It holds the sign-in accounts' password hashes and every
record. With the secrets ticked, it also holds the API keys and tokens.

## Making one and downloading it

1. Tick the secrets and the downloads if you want them. The secrets are needed to restore
   onto a new server; the downloads only to rebuild the database (`igs rebuild`), and they
   can be many gigabytes.
2. Press **Make a backup now**. It runs in the background, outside the app (a transient
   systemd unit on the server), so restarting the app doesn't stop it. Its progress is in
   the log at the bottom of the section.
3. When it is complete it is listed. Press **Download** to save it on your computer.

Details:
- **Space:** the backup is written as a partial file and renamed when complete, so a
  listed backup is always whole. It checks the disk twice: before the database dump
  (which is at most the database's size) and, once the dump's real size is known, before
  writing the backup file. The dump is deleted as soon as it is in the file, so the two
  never take the space at once for long.
- **The download** streams from disk, at any size. The download and upload are plain web
  routes next to the sign-in page (`/backups/...`); Streamlit's own widgets keep a file in
  memory, which a multi-gigabyte backup can't use.
- **Who:** only a signed-in administrator can download or upload.
- **Without password sign-in** (`IGS_AUTH_MODE=local`, the app on your own computer), the
  page shows where each backup is instead (`data/backups`).
- **How many are kept:** the daily cleanup keeps the newest three made or uploaded here
  (`app_backups_keep`, Settings > Maintenance). Download the ones to keep elsewhere.

## Uploading one and restoring it

1. **Upload.** Press **Upload a backup from your computer** and choose the file. It streams
   to the server, with a progress bar. It is kept only if it is a backup (its manifest
   first), as `igs-backup-<its time>-uploaded.tar.gz`. Without password sign-in, copy the
   file into `data/backups` instead.
2. **Choose.** Choose it under **Restore a backup**: the page shows when and where it was
   made, its commit, and its migrations against this code's.
3. **Restore.** Tick its secrets or downloads if you want those back too, type RESTORE, and
   press **Restore this backup**. It runs in the background like a backup.

What a restore does, in order:
1. **Checks the file.** It must be a backup, its database dump must match its checksum, and
   its database must not be from newer app code than this. If it is, deploy that code
   first. The settings and files are read out of it too, into a staging folder: a damaged
   file stops the restore here, before anything has changed.
2. **Makes a safety backup** of the current database, files and secrets
   (`…-before-restore.tar.gz`), so the restore can itself be undone by restoring that.
3. **Pauses the scheduled jobs:** it stops the timers and waits for running jobs (up to 30
   minutes). It also holds the data checks' lock.
4. **Replaces the database in one transaction:** the app's schema is dropped and the dump
   replayed. If anything fails, everything is rolled back and the database stays as it
   was. Migrations then bring it up to this code. If something holds the database for
   more than a minute (an `igs api` left running, say), it stops and says so.
5. **Puts the settings back as they were at the backup** (a setting changed since goes
   back to its default) and writes the other files back.
6. **The secrets and the downloads** only if ticked. Downloads are added to `data/raw`;
   files already there are never overwritten.
7. **Starts the scheduled jobs again,** whatever happened. Which timers it paused is
   written to `data/backups/paused-timers.txt` first; if the restore is killed before its
   end, the next daily cleanup (or the next restore) starts them again from that record.

After a restore:
- **Signing in:** you may need to sign in again, since the accounts and their sessions
  are the backup's.
- **The code** is not restored; it comes from GitHub (docs/GITHUB_DEPLOYMENT.md). A backup
  from older code is brought up to date by the migrations.

## From the command line

```bash
uv run igs backup create [--with-secrets] [--with-raw]   # into data/backups
uv run igs backup list
uv run igs backup restore igs-backup-….tar.gz --yes [--with-secrets] [--with-raw]
```

`restore` takes a backup's name in `data/backups` or a path to one. It does nothing
without `--yes`.

A job started from the page runs through `scripts/igs-job.sh`, outside the app. It gets
the app's directory settings, if any are set for the app (`IGS_BACKUP_DIR`,
`IGS_RAW_ROOT`, `IGS_SETTINGS_DIR`, `IGS_ENV_FILE`); secrets it reads from `.env` itself.

**What it needs:** PostgreSQL's client tools (`pg_dump`, `pg_restore`, `psql`), which the
deployment already checks for. It connects as the app's own database user
(`IGS_DATABASE_URL`), which owns the database. Neither has been tried on Windows.

**This is not the deployment's backup.** The deployment script keeps its own backup in
`/home/anant/deploy-backups`, which belongs to root ([MAINTENANCE.md](MAINTENANCE.md)).
