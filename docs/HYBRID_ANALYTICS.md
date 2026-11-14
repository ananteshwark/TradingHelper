# Local-first nightly research analytics

The production server remains responsible for ingestion, live scans, alerts and orders.
The local computer runs a read-only comparison of completed, costed paper calls after
its daily database mirror refresh. It uploads only the aggregate JSON report. At 22:00
Asia/Kolkata the server checks for a valid same-day local report and computes the same
report from its own database if the computer was off, the mirror was stale, the local
analysis failed, or the upload did not arrive.

The local timer runs at 20:30, after the 19:00 mirror sync. The server stores one
`reports/hybrid-analytics/YYYY-MM-DD.json` file per day with `source: local` or
`source: server`. A local report requires a same-day successful mirror sync. The
handoff uses a lock and an atomic file replacement: a local result that arrives first
wins, and a late upload cannot replace a server fallback. The fallback timer has
`Persistent=true`, so a missed run starts when the server next starts.
If the local computer starts after its scheduled sync, the analytics worker first
starts and waits for a fresh mirror sync rather than using yesterday's data.
The receiver also checks that the local analysis code has the same hash as the
server's copy; if the local checkout is behind, the server runs the fallback.

The report groups morning, evening and spike paper calls by buy/short side. It records
completed calls, winners, net rupees, dates and numeric feature medians for groups
with at least three winners and three losers. Stock, previous-session and market
features are labelled. These are descriptions, not validated entry filters. No
model is trained, promoted or used for live calls by this handoff; the current
historical label coverage and benchmark gaps still block automatic promotion.

## One-time setup

Deploy `codex/server-app` first. Install the root-owned SSH gate on the server and
replace only the forced command on the existing local mirror export key in
`/root/.ssh/authorized_keys`:

```bash
install -d -o root -g root -m 755 /usr/local/libexec
install -o root -g root -m 755 \
  /home/anant/TradingHelper/scripts/research-ssh-gate.sh \
  /usr/local/libexec/igs-research-ssh-gate
```

The key line must keep SSH's `restrict` option and use
`command="/usr/local/libexec/igs-research-ssh-gate"`. The gate accepts exactly
`export` (the old read-only `pg_dump`) or `upload YYYY-MM-DD` (a bounded, validated
aggregate report written as `anant`). It permits no shell, port forwarding or
arbitrary file path. After installing, test the existing mirror sync and a local
report upload before enabling the server fallback timer.

The server's existing `scripts/install-schedules.sh` installs and enables
`igs-hybrid-analytics.timer` with the other `igs-*` timers. On the local computer,
check out `codex/local-app`, copy
`deploy/local-research/tradinghelper-research-analytics.{service,timer}` into
`~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then enable and
start `tradinghelper-research-analytics.timer`. The local user service manager has
lingering enabled, so the timer works whenever the computer is on without a login.

## Checks

```bash
# Local computer
systemctl --user start tradinghelper-research-analytics.service
journalctl --user -u tradinghelper-research-analytics.service -n 50 --no-pager

# Server, as anant
systemctl --user start igs-hybrid-analytics.service
cat ~/TradingHelper/reports/hybrid-analytics/$(TZ=Asia/Kolkata date +%F).json
```

A `source: local` report confirms offload; `source: server` confirms fallback. The
server timer must remain enabled, even if the local computer is normally on. This
handoff never changes the production database or the active model. Compare a
candidate against independent later dates and after charges before a separate,
reviewed model deployment.
