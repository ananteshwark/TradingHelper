# Move TradingHelper to a new Ubuntu server, including all data

Prepared for the installation inspected on 2 October 2026. These are instructions;
preparing this guide did not stop the application, export data, or change servers.

Assumptions: a fresh Ubuntu 24.04 or 26.04 server, SSH and sudo access, and Linux
login `anant` on both machines. Keep the project at `/home/anant/TradingHelper`.
Replace `NEW_SERVER_IP` below with the new server's address. If your destination
uses a different OS or username, adapt these commands before running them.

The inspected installation has PostgreSQL **18.6**, database/role **igs**, about
**2.1 GB** in PostgreSQL and **4.3 GB** in `data/`. Raw-data paths in the database
are relative to the raw root. `IGS_RAW_ROOT`, `IGS_SETTINGS_DIR`, `IGS_CONFIG_DIR`
and `IGS_ENV_FILE` currently use their defaults. Five user timers are installed:
`igs-daily`, `igs-sync`, `igs-news`, `igs-call-reviews`, and `igs-verify`.

This guide preserves code and Git history, `.env`, settings, raw files, reports,
logs, the complete database (including AI history, watchlists, broker calls and
Telegram deduplication/outbox state), and installed schedules. It recreates the
Python environment. External personal files referenced outside the repository,
if any, must be transferred separately.

Use PostgreSQL 18 on the destination. Do not use the PostgreSQL 16 instructions
in the older deployment guide for this existing database. Also, do not rely on
a fresh GitHub clone: local changes through `660d73f` had not been pushed when
this guide was prepared.

## 1. Prepare the NEW server before downtime

Log into the new server as `anant`. If that user does not exist, create it from
your administrator account with `sudo adduser anant` and
`sudo usermod -aG sudo anant`, and configure its SSH access first.

```bash
sudo apt update
sudo apt install -y curl ca-certificates git rsync postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt update
sudo apt install -y postgresql-18 postgresql-client-18
sudo systemctl enable --now postgresql
pg_lsclusters
pg_dump --version
```

Confirm a PostgreSQL 18 cluster is online on port 5432. If another cluster already
uses that port, resolve the port choice before proceeding; do not remove an
existing database. These steps assume the destination has no existing `igs` DB.

Install uv as `anant`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
mkdir -p ~/migration-incoming
chmod 700 ~/migration-incoming
```

Allow disk space for the extracted application, live database, migration archive,
database dump, and growth. For this installation, start with at least 30 GB free
and check `df -h ~ /var/lib/postgresql`. This is headroom, not a measured capacity
requirement. Avoid opening PostgreSQL port 5432 or dashboard port 8501 publicly;
the access steps below use SSH forwarding.

## 2. Pause writes on the OLD server

Schedule downtime. Run these on the old desktop as `anant`:

```bash
cd ~/TradingHelper
umask 077
MIGRATION_DIR="$HOME/tradinghelper-migration-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$MIGRATION_DIR/systemd"
cp -a ~/.config/systemd/user/igs-* "$MIGRATION_DIR/systemd/"
systemctl --user list-timers 'igs-*' --all --no-pager > "$MIGRATION_DIR/timers-before.txt"
git rev-parse HEAD > "$MIGRATION_DIR/code-commit.txt"
systemctl --user disable --now igs-daily.timer igs-sync.timer igs-news.timer igs-call-reviews.timer igs-verify.timer
systemctl --user list-units 'igs-*.service' --state=activating,running --no-pager
```

Let any running ingestion, scoring, news, or AI review service finish. Check the
last command again until no application job is running. Stopping timers does
**not** stop a service already running.

Stop the dashboard and its launcher (Ctrl+C in its terminal, or stop its systemd
service if you installed one). Also stop any manually started CLI, API or
background sync process. The dashboard can start sync work, so closing the
browser alone is insufficient. Check candidates without killing unrelated apps:

```bash
pgrep -af 'igs.cli|streamlit|igs sync|igs daily|igs news'
crontab -l
```

Inspect matching processes; stop only TradingHelper processes and pause any
separate cron/system-level TradingHelper schedules you find. No application
writes or Telegram sends should occur between the final backup and cutover.
Keep PostgreSQL itself running for the export.

## 3. Export the database and package files on the OLD server

In the same terminal (so `MIGRATION_DIR` remains set):

```bash
cd ~/TradingHelper
pg_dump --version
pg_dump -h localhost -p 5432 -U igs -W -d igs -Fc -f "$MIGRATION_DIR/igs.dump"
pg_restore --list "$MIGRATION_DIR/igs.dump" > "$MIGRATION_DIR/dump-contents.txt"
```

The password prompt requires the **database role's password from your current
`.env`**, not your Linux or GitHub password. The command assumes the local DB is
on port 5432; use the host/port in `IGS_DATABASE_URL` if customized. Use the
PostgreSQL 18 `pg_dump` binary. Stop if either command reports an error.

Create a reusable verification script. It counts every application table and
checks that every recorded raw payload has its file. It does not print keys:

```bash
cat > "$MIGRATION_DIR/verify-data.py" <<'PY'
import json
import os
from pathlib import Path
from psycopg import sql
from igs import envfile
from igs.db import connect

envfile.load(envfile.default_path())
root = Path(os.environ.get('IGS_RAW_ROOT', Path.cwd() / 'data/raw'))
with connect() as conn:
    tables = conn.execute("select tablename from pg_tables where schemaname='public' order by tablename").fetchall()
    counts = {}
    for (name,) in tables:
        counts[name] = conn.execute(sql.SQL('select count(*) from public.{}').format(sql.Identifier(name))).fetchone()[0]
    missing = sum(not (root / row[0]).is_file() for row in conn.execute('select blob_path from raw_payload'))
print(json.dumps({'table_counts': counts, 'missing_raw_files': missing}, indent=2, sort_keys=True))
PY
uv run python "$MIGRATION_DIR/verify-data.py" > "$MIGRATION_DIR/before.json"
```

Review `before.json`. If raw files are already missing, investigate before
moving; a database export cannot recreate missing files.

Package the entire local working copy, including hidden settings and local Git
commits. Do not copy the running PostgreSQL data directory or the old `.venv`:

```bash
tar --exclude='TradingHelper/.venv' --exclude='TradingHelper/.pytest_cache' --exclude='TradingHelper/.ruff_cache' --exclude='*/__pycache__' --exclude='TradingHelper/.agents' --exclude='TradingHelper/.claude' --exclude='TradingHelper/.codex' -czf "$MIGRATION_DIR/TradingHelper.tar.gz" -C "$HOME" TradingHelper
cd "$MIGRATION_DIR"
sha256sum igs.dump TradingHelper.tar.gz > SHA256SUMS
chmod 600 igs.dump TradingHelper.tar.gz
```

The excluded agent/editor folders are development tooling, not application data.
The archive includes `.git`, `.env`, `.streamlit`, `config/`, `data/`, `reports/`
and `logs/`. If you add custom external storage paths before migration, back up
those locations too. Keep the archive private: it contains API tokens/passwords.

## 4. Transfer to the NEW server

From the old server, in the same terminal:

```bash
rsync -avP "$MIGRATION_DIR/" anant@NEW_SERVER_IP:~/migration-incoming/
```

On the new server:

```bash
cd ~/migration-incoming
sha256sum -c SHA256SUMS
test ! -e "$HOME/TradingHelper"
tar -xzf TradingHelper.tar.gz -C "$HOME"
chmod 600 ~/TradingHelper/.env
cd ~/TradingHelper
git rev-parse HEAD
cat ~/migration-incoming/code-commit.txt
uv sync --locked --all-groups
```

Both checksums must say `OK`; the commit IDs must match. If `~/TradingHelper`
already exists, stop and choose a fresh destination rather than extracting over
it. `test` returning failure means stop; it does not itself stop an interactive
shell. Do not run migrations or the dashboard before restoring the database.

## 5. Restore into an EMPTY database on the NEW server

Create a new role and set its password interactively (if it already exists,
inspect its purpose before changing it):

```bash
sudo -u postgres createuser --login igs
sudo -u postgres psql -c '\password igs'
sudo -u postgres createdb --owner=igs --template=template0 igs
pg_restore -h localhost -p 5432 -U igs -W -d igs --no-owner --no-acl --exit-on-error --single-transaction ~/migration-incoming/igs.dump
```

Choose the same database password as before to keep the copied `.env` usable,
or edit just `IGS_DATABASE_URL` in `~/TradingHelper/.env` for the new password.
Preserve the Anthropic/Telegram and other credentials. URL-encode special
characters in a database URL password. Do not paste credentials into Git or chat.
The restore uses the destination `igs` role as owner. PostgreSQL roles are
recreated here because a single-database dump does not include global roles.

Do not run `igs db migrate` into the empty database before restoration. Do not
use `igs rebuild`: raw-file replay does not replace a full restore of user
state, AI verdicts, historical rankings and alert delivery records. If restore
fails, investigate the error; do not proceed with a partially prepared server.

## 6. Verify the restored data before enabling work

On the new server:

```bash
cd ~/TradingHelper
unset IGS_DATABASE_URL
uv run python ~/migration-incoming/verify-data.py > ~/migration-incoming/after.json
diff -u ~/migration-incoming/before.json ~/migration-incoming/after.json
uv run igs db migrate
uv run igs db status
uv run igs gate run
```

Unsetting the shell variable makes the CLI use the restored `.env`. Also remove
any stale shell/service overrides of custom environment/config/raw/settings
paths. An empty `diff` means all table counts and missing-file counts match.
With matching code, migrations should normally report up to date. The gate
validates the code on the new machine. Do not run database tests against the
live `igs` database.

Start a temporary dashboard with background ingestion disabled:

```bash
uv run igs ui --no-sync
```

From your desktop, open a second terminal:

```bash
ssh -N -L 8502:127.0.0.1:8501 anant@NEW_SERVER_IP
```

Open **http://localhost:8502** on the desktop. Verify rankings, historical runs,
Broker/AI calls, confidence, performance columns, watchlists, news and Settings.
Avoid review/import buttons during validation: they can modify data or send
alerts. Keep this tunnel open while browsing.

Verify feeds from the new server before permanent cutover:

```bash
uv run igs sources verify
```

This contacts sources and stores verification results, so perform the count
comparison first. Cloud egress can behave differently from your desktop; do not
assume that being able to load the dashboard proves NSE/news downloads work.
Resolve blocked sources or choose a supported/licensed source before relying on
automatic updates. A successful endpoint check still does not guarantee every
future download.

## 7. Enable the dashboard and schedules on the NEW server

Stop the temporary dashboard with Ctrl+C. Install a user service to keep the UI
running; the separate schedules will handle background ingestion:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/igs-ui.service <<'UNIT'
[Unit]
Description=TradingHelper dashboard

[Service]
WorkingDirectory=%h/TradingHelper
ExecStart=%h/TradingHelper/.venv/bin/python -m igs.cli ui --no-sync
Restart=on-failure
RestartSec=10
KillMode=control-group

[Install]
WantedBy=default.target
UNIT
```

Restore the five known schedules exactly as they were configured on the old
machine. Inspect any custom absolute paths before enabling them:

```bash
for name in daily sync news call-reviews verify; do
    install -m 0644 "$HOME/migration-incoming/systemd/igs-$name.service" "$HOME/.config/systemd/user/igs-$name.service"
    install -m 0644 "$HOME/migration-incoming/systemd/igs-$name.timer" "$HOME/.config/systemd/user/igs-$name.timer"
done
sudo loginctl enable-linger anant
systemctl --user daemon-reload
systemctl --user enable --now igs-ui.service
systemctl --user enable --now igs-daily.timer igs-sync.timer igs-news.timer igs-call-reviews.timer igs-verify.timer
systemctl --user list-timers 'igs-*' --all --no-pager
systemctl --user status igs-ui.service --no-pager
curl -fsS http://127.0.0.1:8501/_stcore/health
```

Persistent timers may immediately run a missed job. The restored outbox preserves
sent-message history, but legitimately pending/failed notifications can now be
sent. Keep all five timers **disabled on the old machine**, and do not reopen its
UI with background sync. Running two copies can send duplicate Telegram alerts
and incur duplicate AI charges even though their initial databases were identical.

Send one explicit test notification and check the new worker logs:

```bash
cd ~/TradingHelper
uv run igs alerts --test-telegram
journalctl --user -u igs-call-reviews.service -n 30 --no-pager
tail -n 30 logs/brokers.log
```

Continue accessing the dashboard through the SSH tunnel. Do not expose its
settings/API-token controls by simply binding it to `0.0.0.0`; use authenticated
access if you later require a shared web deployment.

## 8. Retain rollback and backups

Keep the stopped old installation and private migration archive until the new
server has completed successful ingestion, scoring, AI review and Telegram runs.
Confirm source-file dates advance and the next scheduled ranking completes.
Set up regular backups of **both PostgreSQL and the application data/settings**
on the new server, including an off-server copy, and test restoration.

Before the new server has accepted writes, rollback is simply: stop its UI and
disable its timers, then enable the five old timers and restart the old UI.
Once the new server has accepted writes or sent alerts, the old copy is stale.
Stop the new instance and transfer its latest database/files back before resuming
old-server operations if you need to preserve those changes and delivery history.
Do not run both or try to merge PostgreSQL data folders.

## References

- PostgreSQL's [Ubuntu repository instructions](https://www.postgresql.org/download/linux/ubuntu/) support selecting version 18 explicitly.
- PostgreSQL 18 [pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html) documents custom-format exports and the scope of a single-database dump.
- PostgreSQL 18 [pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html) documents restoring archives, ownership options, and transaction/error handling.
- Astral's [uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/) provide the installer used above.
