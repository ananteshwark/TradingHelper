# Deploy from GitHub

On the production server, sign in as root and run:

```bash
bash /home/anant/TradingHelper/scripts/deploy-from-github.sh
```

For a shorter command, install a link once (the script still comes from GitHub):

```bash
ln -s /home/anant/TradingHelper/scripts/deploy-from-github.sh /usr/local/bin/igs-update
igs-update
```

To check Git, the checkout and service readiness without deploying:

```bash
bash /home/anant/TradingHelper/scripts/deploy-from-github.sh --check
```

The script deploys the fetched commit on `claude/india-growth-screener-32wchm`.
It requires a clean checkout and a fast-forward, serializes deployments, pauses
active schedules and waits up to 30 minutes for background jobs to finish. It
backs up the `igs` database and previous committed source under
`/home/anant/deploy-backups`, then updates dependencies, migrates, runs the
look-ahead gate, restarts the dashboard and checks its local health endpoint.
Only schedules that were active before deployment are restarted. Existing
installed service units and server secrets are retained.

If validation fails after code changes, the dashboard and schedules remain
stopped. Inspect the error before restarting them; do not automatically downgrade
code across a database migration. The backup directory records the previous and
target commits and the active timer names. If the failure occurs before code
changes, the schedules are restored.

Make source changes in development, test and push to GitHub, then deploy with this
command. Do not edit source files directly on the server or apply old Git stashes
over the deployed checkout.
