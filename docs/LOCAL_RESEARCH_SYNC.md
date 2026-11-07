# Local research mirror and training console

The production PostgreSQL database is authoritative. A dedicated SSH key can only run
`pg_dump` on the server; it cannot start an interactive shell or forward a port. The
computer downloads a consistent custom-format dump every day at 19:00 Asia/Kolkata,
restores it into a separate PostgreSQL 18 cluster, checks the schema, and swaps the
local `igs_research` database into place. The previous successful local copy is kept
as `igs_research_previous`. A failed transfer or restore leaves the current copy intact.
The sync does not send local changes to production.

After a successful same-day sync, the local computer runs a read-only paper-call
analysis at 20:30 and uploads an aggregate report. If it is off or the result is
missing, the server runs the same report at 22:00. See
[HYBRID_ANALYTICS.md](HYBRID_ANALYTICS.md) for the handoff and its checks. Live
scans and calls continue on the server.

The local console is available at <http://127.0.0.1:8502> and listens only on the
computer's loopback interface. It shows net paper outcomes, side-specific numeric
feature differences, and background jobs for the existing point-in-time feature
exporter and chronological model trainer. It does not show passwords or write into
the production app. Candidate model artifacts are saved under
`reports/local-research/train/` and include validation/holdout diagnostics. They are
not automatically used for trading.

**Current data check (7 November 2026):** A monthly 21-session export from January
2025 through September 2026 produced 2,243 feature rows, but only four signal dates
had matured labels. The trainer requires at least 12 earlier training dates and two
dates in each later block, so a new model cannot be validated from this history yet.
The Nifty 500 total-return benchmark is also missing; the available price index
overstates excess returns. The console shows both limitations and disables the
training action for this dataset. More dated historical inputs and independent
future outcomes are needed before a replacement model can be used for server calls.

## Local installation (Linux)

This checkout expects the owner's home at `/home/anant/TradingHelper` and PostgreSQL
18 binaries in `/usr/lib/postgresql/18/bin`. The per-machine SSH key and database
files live under ignored `data/` paths. Keep the private key, dump and local database
private; the dump contains accounts, broker records and other app data. The key on
the server has a forced, read-only export command. Do not copy it into Git.

```bash
uv sync --locked --all-groups
install -d -m 700 data/research-sync data/research-pg/socket
ssh-keygen -t ed25519 -N '' -f data/research-sync/id_ed25519 -C igs-research-sync
chmod 600 data/research-sync/id_ed25519
/usr/lib/postgresql/18/bin/initdb -D data/research-pg/data -A trust -U "$(id -un)"
cat >> data/research-pg/data/postgresql.conf <<'CONF'
listen_addresses = ''
unix_socket_directories = '/home/anant/TradingHelper/data/research-pg/socket'
port = 55438
CONF
mkdir -p ~/.config/systemd/user
cp deploy/local-research/*.service deploy/local-research/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tradinghelper-research-db.service
PGHOST="$PWD/data/research-pg/socket" PGPORT=55438 createdb igs_research
systemctl --user enable --now tradinghelper-research-ui.service
systemctl --user enable --now tradinghelper-research-sync.timer
systemctl --user enable --now tradinghelper-research-analytics.timer
systemctl --user start tradinghelper-research-sync.service  # optional immediate refresh
systemctl --user status tradinghelper-research-sync.service
```

On the server, add the generated public key to root's `authorized_keys` with this
forced command and SSH's `restrict` option (replace `PUBLIC_KEY` with the one line in
`data/research-sync/id_ed25519.pub`):

```text
restrict,command="/usr/sbin/runuser -u postgres -- /usr/lib/postgresql/18/bin/pg_dump -Fc --no-owner --no-acl igs" PUBLIC_KEY
```

Do this once; do not add the private key to the server. If the local cluster and key
already exist, keep them and skip the initialization and key-generation commands.

The timer needs the computer to be on or user lingering enabled. `Persistent=true`
starts a missed sync after the computer next starts. Check
`journalctl --user -u tradinghelper-research-sync.service -n 100` and
`data/research-sync/status.json` for freshness and errors. The SSH connection pins the
server through the normal `known_hosts` file and refuses unknown or changed host keys.
Only one sync runs at a time. If a local research query or export is still using the
current database, the sync keeps that database and retries at the next run; start the
service manually to retry sooner.

The local research console can launch `igs gate run`, `igs research features`,
`igs research train`, `igs research predict`, and `igs research portfolio` against
the mirror. The feature bundle and model manifests carry source hashes and label
timing. Predictions require a later dataset whose first signal is after the last
training label. Inspect both chronological splits, date-level coverage, drift, and
the costed portfolio result before suggesting a change to future calls. The
feature exporter supports daily, weekly and monthly signals; the costed portfolio
simulator currently supports monthly signals only. The
existing `igs research readiness` command records missing promotion evidence.
The current paper-call winner/loser report already showed that simple filters
reduced later profits; a new model needs independent forward evidence.

To inspect the local database directly:

```bash
PGHOST="$PWD/data/research-pg/socket" PGPORT=55438 psql -d igs_research
```

Never set `IGS_DATABASE_URL` in the server's `.env` to this mirror. The application on
the server continues to use its own database and existing validated models.
