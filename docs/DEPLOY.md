# Installing and running IndiaGrowthScreener on a Windows or Ubuntu desktop

> Personal research tool. Not investment advice. Keep its output for your own use:
> distributing it to others may attract SEBI Research Analyst obligations.

When you finish you will have:

- a PostgreSQL 16 database on your computer;
- the app in a folder, with its settings in one file (`.env`);
- the rankings in your browser at http://localhost:8501, visible only on your computer;
- a job that runs every weekday evening to fetch the day's data, re-score and send alerts.

How far this has been tested:

- **Ubuntu:** these steps were run on Ubuntu 24.04 with PostgreSQL 16, from a fresh clone: database and user, settings file, migrations, look-ahead gate, source verification against NSE, loading, `db status`, scoring, UI and API (both listening on 127.0.0.1 only) and the job script. The systemd timer files were checked with `systemd-analyze` but not run in a desktop session. The apt and uv installs were already present.
- **Windows:** the steps have not been run on a Windows computer. They use the standard installers and PowerShell commands. The code paths that behave differently on Windows were fixed for it: text encoding, time zones, line endings and the scheduled-job script. If a step fails, the troubleshooting table at the end covers the likely causes.

Part 1 is for Ubuntu and Part 2 for Windows. Part 3, the first data load, is the same on both, and so are Parts 4 and 5.

---

## Before you start

- **Network.** NSE serves its data to ordinary Indian internet connections but refuses many cloud and data-centre addresses, and some VPNs. Run the app from your home or office connection, with any VPN off.
- **NSE's terms of use.** NSE's website terms forbid systematic or automated data collection without NSE's express written consent. This app collects its data from nseindia.com automatically. Spacing the requests out reduces the load but is not consent. For anything beyond trying the app, ask NSE for consent or use a licensed data feed. The decision is yours; see "Known limitations" in the README.
- **NSE rate limits.** The app waits 5 seconds between requests to NSE. If NSE still answers "403 Access Denied", the app pauses for about 5½ minutes and tries once more. Those pauses are normal, so don't interrupt them.
- **Time for the first load.** Loading a full history is slow because of that spacing (details in Part 3):
  - prices: about 1 hour per year of history;
  - results documents: about 1½ days for everything filed since March 2025.

  Plan to leave it running over a few evenings. After that, the daily job takes minutes, or longer in results season.
- **Disk.** Allow 20 GB free:
  - prices: about 0.75 MB of files per trading day, so roughly 2–3 GB for 12 years;
  - results documents: 20–100 KB each;
  - daily listing snapshots: 2–3 GB a year.
- **How much results history can be loaded today.** Results filed under NSE's old system can be listed only for the December-2024 quarter; earlier quarters need a dated query that hasn't been confirmed yet. Integrated Filing covers March 2025 onwards, so the database will hold about 7 quarters of results. The universe requires 8 quarters (`config/universe.yaml`), so no company qualifies for ranking until the September-2026 results are filed, by mid-November 2026. Part 3.4 shows how to take a provisional look before then.

---

## Part 1: Ubuntu (24.04; notes for 22.04)

Use a terminal. Commands that start with `sudo` ask for your login password.

### 1.1 Install Git, curl and PostgreSQL 16

Ubuntu 24.04 ships PostgreSQL 16:

```bash
sudo apt update
sudo apt install -y git curl postgresql
psql --version          # should print 16.x
```

Ubuntu 22.04 ships PostgreSQL 14. Install 16 from the PostgreSQL project's own repository instead, and don't install the plain `postgresql` package as well: two versions would compete for port 5432.

```bash
sudo apt update
sudo apt install -y git curl postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt install -y postgresql-16
pg_lsclusters           # the 16 cluster should be "online" on port 5432
```

### 1.2 Install uv (it also installs Python 3.12 for the app)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"       # or open a new terminal
uv --version
```

### 1.3 Create the database and its user

Choose a password without `@ : / ? # %`, because it goes into a URL.

```bash
sudo -u postgres psql -c "CREATE ROLE igs LOGIN PASSWORD 'choose-a-password';"
sudo -u postgres createdb -O igs igs
```

### 1.4 Get the code

```bash
cd ~
git clone -b main https://github.com/ananteshwark/TradingHelper.git
cd TradingHelper
```

If the repository is private, Git asks for your GitHub username and, as the password, a personal access token (GitHub → Settings → Developer settings → Personal access tokens).

### 1.5 Create the settings file

The app reads `.env` from its folder. Variables already set in your environment take precedence over it.

```bash
cat > .env <<'EOF'
IGS_DATABASE_URL=postgresql://igs:choose-a-password@localhost:5432/igs
EOF
chmod 600 .env          # it holds passwords: readable by you only
```

### 1.6 Install the app and prepare the database

```bash
uv sync --all-groups    # Python 3.12 and every dependency, including the UI
uv run igs db migrate   # creates the tables; prints "applied: 001_raw_and_dq, ..."
uv run igs gate run     # about a minute; prints "look-ahead gate PASSED ..."
uv run igs db status    # the database is reachable; every table is still empty
```

The look-ahead gate proves the scoring code only ever sees data that was public on each date. Scoring refuses to run until it has passed for the current code.

Now do **Part 3, the first data load**, then come back to 1.7.

### 1.7 Schedule the daily job (systemd timer)

A systemd timer runs the job on weekdays at 20:30 IST, after NSE publishes the day's files. It re-verifies the sources on Saturdays. If the computer was off at that time, the job runs when it is next on.

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/igs-daily.service <<'EOF'
[Unit]
Description=IndiaGrowthScreener: daily ingest, score and alerts

[Service]
Type=oneshot
ExecStart=%h/TradingHelper/scripts/igs-job.sh daily
EOF

cat > ~/.config/systemd/user/igs-daily.timer <<'EOF'
[Unit]
Description=Run IndiaGrowthScreener after NSE's evening files (weekdays)

[Timer]
OnCalendar=Mon..Fri 20:30 Asia/Kolkata
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > ~/.config/systemd/user/igs-verify.service <<'EOF'
[Unit]
Description=IndiaGrowthScreener: re-verify every data source

[Service]
Type=oneshot
ExecStart=%h/TradingHelper/scripts/igs-job.sh sources verify
EOF

cat > ~/.config/systemd/user/igs-verify.timer <<'EOF'
[Unit]
Description=Re-verify IndiaGrowthScreener's sources weekly

[Timer]
OnCalendar=Sat 08:00 Asia/Kolkata
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now igs-daily.timer igs-verify.timer
sudo loginctl enable-linger "$USER"     # keep the timers running when you are logged out
systemctl --user list-timers            # shows the next run of each
```

To run the job now and watch it:

```bash
systemctl --user start --no-block igs-daily.service
tail -f ~/TradingHelper/logs/daily.log        # Ctrl+C stops watching, not the job
```

Cron works too: run `crontab -e` and paste the two lines from `scripts/crontab.example`. Cron doesn't catch up on runs missed while the computer was off.

### 1.8 Open the app

```bash
cd ~/TradingHelper
uv run igs ui           # opens http://localhost:8501 in your browser; Ctrl+C stops it
uv run igs api          # optional, in another terminal: http://localhost:8000/docs
```

Both listen on this computer only. Use `--port` if the default port is taken.

---

## Part 2: Windows 10 or 11

Use **PowerShell** (Start → type "PowerShell"), not Command Prompt.

### 2.1 Install Git and uv

```powershell
winget install --id Git.Git -e --source winget
winget install --id astral-sh.uv -e
```

Close PowerShell and open it again, then check that both are found:

```powershell
git --version
uv --version
```

If `winget` isn't available, install Git from https://git-scm.com/download/win and uv with:
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

### 2.2 Install PostgreSQL 16

1. Open https://www.postgresql.org/download/windows/, choose "Download the installer" and download version 16.x for Windows x86-64.
2. Run the installer:
   - keep the default components (you can untick Stack Builder);
   - set a password for the `postgres` administrator and write it down;
   - keep port 5432.
3. At the end, untick "Launch Stack Builder".

PostgreSQL now runs as a Windows service and starts with Windows.

### 2.3 Create the database and its user

Choose a password without `@ : / ? # %`, because it goes into a URL. Each command asks for the `postgres` password from 2.2.

```powershell
$psql = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
& $psql -U postgres -c "CREATE ROLE igs LOGIN PASSWORD 'choose-a-password';"
& $psql -U postgres -c "CREATE DATABASE igs OWNER igs;"
```

### 2.4 Get the code

```powershell
cd $HOME
git clone -b main https://github.com/ananteshwark/TradingHelper.git
cd TradingHelper
```

If the repository is private, a browser window opens so you can sign in to GitHub.

### 2.5 Create the settings file and switch Python to UTF-8

```powershell
Set-Content -Path .env -Encoding utf8 -Value "IGS_DATABASE_URL=postgresql://igs:choose-a-password@localhost:5432/igs"
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")
```

`PYTHONUTF8=1` stops Windows' older default encoding from garbling company names and filing text. Close PowerShell, open it again, and go back to the folder:

```powershell
cd $HOME\TradingHelper
```

To change a setting later, run `notepad .env`.

### 2.6 Install the app and prepare the database

```powershell
uv sync --all-groups    # Python 3.12 and every dependency, including the UI
uv run igs db migrate   # creates the tables; prints "applied: 001_raw_and_dq, ..."
uv run igs gate run     # about a minute; prints "look-ahead gate PASSED ..."
uv run igs db status    # the database is reachable; every table is still empty
```

The look-ahead gate proves the scoring code only ever sees data that was public on each date. Scoring refuses to run until it has passed for the current code.

Now do **Part 3, the first data load**, then come back to 2.7.

### 2.7 Schedule the daily job (Task Scheduler)

These commands create two tasks:
- **IGS daily:** weekdays at 20:30, after NSE publishes the day's files.
- **IGS weekly verify:** Saturdays at 08:00.

The times are in your computer's time zone. If it isn't set to India Standard Time, convert them (20:30 IST is 15:00 UTC). A task that was missed because the computer was off runs when it is next on. Tasks run while you are logged in.

```powershell
cd $HOME\TradingHelper
$repo = (Get-Location).Path
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

$daily = New-ScheduledTaskAction -Execute "$repo\scripts\igs-job.cmd" -Argument "daily" -WorkingDirectory $repo
$weekdays = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "20:30"
Register-ScheduledTask -TaskName "IGS daily" -Action $daily -Trigger $weekdays -Settings $settings

$verify = New-ScheduledTaskAction -Execute "$repo\scripts\igs-job.cmd" -Argument "sources verify" -WorkingDirectory $repo
$saturday = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At "08:00"
Register-ScheduledTask -TaskName "IGS weekly verify" -Action $verify -Trigger $saturday -Settings $settings
```

To run the job now and watch it:

```powershell
Start-ScheduledTask -TaskName "IGS daily"
Get-Content logs\daily.log -Tail 30 -Wait
```

To remove a task: `Unregister-ScheduledTask -TaskName "IGS daily" -Confirm:$false`.

### 2.8 Open the app

```powershell
cd $HOME\TradingHelper
uv run igs ui           # opens http://localhost:8501 in your browser; Ctrl+C stops it
uv run igs api          # optional, in another window: http://localhost:8000/docs
```

Both listen on this computer only. Use `--port` if the default port is taken.

---

## Part 3: The first data load (same commands on both)

Run these in the `TradingHelper` folder, in a terminal on Ubuntu or PowerShell on Windows. Keep the computer awake: turn off sleep while this runs.

### 3.1 Check which sources answer

```bash
uv run igs sources verify
uv run igs sources list
```

`verify` fetches a real sample from every source and records its format. Ingestion refuses a source that hasn't been verified, and stops if its format changes later. The statuses in `list` mean:

| Status | Meaning |
|---|---|
| `verified` | Ready to load. |
| `failed` | The source didn't answer with data; the message says why. Re-run `uv run igs sources verify <source id>` later. |
| `no url` | Not available yet (delisted securities, the Nifty 500 total-return index). This is expected. |

These sources matter most:
- **Bhavcopy, delivery and index closes:** prices.
- **`nse_financial_results_index` and `nse_integrated_filing_index`:** results.
- **`nse_shareholding_index`:** shareholding.
- **`nse_quote_equity`:** NSE's industry classification. If it fails, the app uses the industry label on each company's NSE announcements instead.

### 3.2 Load history, in this order

Load each static source with its own command, and skip any that isn't `verified`:

```bash
uv run igs ingest static nse_equity_list
uv run igs ingest static nse_trading_holidays
uv run igs ingest static bse_scrip_master
uv run igs ingest static angel_scrip_master
```

**Prices.** Prices take about an hour per year of history, so load them a year at a time, for example one or two years per evening. Two years (below) are enough for momentum and the 200-day average. Older years are needed for the 5-year valuation history and for a meaningful backtest. Set `--end` to the last trading day.

```bash
uv run igs ingest prices --start 2024-09-01 --end 2025-08-31
uv run igs ingest prices --start 2025-09-01 --end 2026-09-23
```

If a price load stops part-way, restart it from the day after the last day loaded rather than repeating the range. Repeating wastes hours, and NSE may refuse repeated requests. `igs db status` shows the last day loaded, the latest filing and the rows in each table:

```bash
uv run igs db status
```

**Corporate actions, announcements and the instrument master:**

```bash
uv run igs ingest range nse_corporate_actions --start 2014-01-01 --end 2026-12-31
uv run igs ingest range nse_announcements --start 2025-01-01 --end 2026-09-23
uv run igs master rebuild
```

**Industry classification.** Run this only if `nse_quote_equity` is verified. It makes one request per company, so allow about 3 hours. Test it on one company first:

```bash
uv run igs ingest symbols nse_quote_equity RELIANCE
uv run igs ingest symbols nse_quote_equity
```

**Results and shareholding listings.** The Integrated Filing listing takes about 2 hours:

```bash
uv run igs ingest static nse_financial_results_index
uv run igs ingest static nse_shareholding_index
uv run igs ingest pages nse_integrated_filing_index --backfill --max-pages 1400
```

**Documents.** The documents themselves take one request per filing: about 30,000 results filings, roughly 1½ days, plus about 2,300 shareholding filings, about 3 hours. Load them in batches, one per evening, until `igs db status` stops showing new filings and a run fetches nothing:

```bash
uv run igs ingest documents financial_results --limit 3000
uv run igs ingest documents shareholding
```

Finish the document backfill before turning on the daily schedule (1.7 or 2.7). Otherwise the first scheduled run tries to fetch all of them at once.

### 3.3 Check the data before trusting it

```bash
uv run igs recon --start 2024-09-01 --end 2026-09-23
uv run igs validate fundamentals
```

- `recon` compares prices, corporate actions and identifiers across sources and reports anything that doesn't reconcile.
- `validate fundamentals` compares loaded results with the hand-checked values in `config/hand_checked.yaml`. The values in that file are left for you to type in from the companies' published results.

### 3.4 Score

```bash
uv run igs score
```

Until 8 quarters of results are loaded (see "Before you start"), the universe is empty. For a provisional look before then:
1. Set `min_filing_quarters: 6` in `config/universe.yaml`. Growth factors that need longer history show "insufficient data".
2. Remember to set it back to 8 later.

The walk-forward backtest (`uv run igs backtest --start ... --end ...`) needs 10 or more years of results to mean anything. Skip it until older quarters can be loaded. Scoring still runs without it, with a warning that the factors aren't IC-validated yet.

### 3.5 Look at it

Open the app (1.8 or 2.8).

---

## Part 4: Everyday use

### What runs when

The daily job does the following:
1. Loads the day's prices, delivery, index closes, corporate actions, announcements, surveillance lists, new results and shareholding filings.
2. Re-scores.
3. Sends alerts.

It logs to `logs/daily.log`, one block per run with each step marked OK or FAILED, and writes alert files to `reports/`.

### Alerts by email or Telegram

Add the channels you want to `.env`, then choose which alerts to send in `config/alerts.yaml`:

```
IGS_SMTP_HOST=smtp.example.com
IGS_SMTP_PORT=587
IGS_SMTP_USER=you@example.com
IGS_SMTP_PASSWORD=your-app-password
IGS_ALERT_FROM=you@example.com
IGS_ALERT_TO=you@example.com
IGS_TELEGRAM_TOKEN=123456:bot-token-from-BotFather
IGS_TELEGRAM_CHAT_ID=your-chat-id
```

Many mail providers (Gmail, Outlook) require an app password here, not your normal password.

### The research assistant (optional, AI)

The assistant answers questions about a run, writes plain-language briefs of a stock's result and reads new announcements. It uses the Claude API, which is billed per use, and is off until you turn it on. It never affects rankings. The README section "Research assistant" says what it does and what it sends to the API.

1. Create an API key at https://console.anthropic.com (Settings → API keys) and add billing credit there.
2. Install the SDK and update the database:
   ```bash
   uv sync --all-groups
   uv run igs db migrate
   ```
3. Start the app (`uv run igs ui`) and open **Settings** in the sidebar.
   - **API key**: paste the key and click **Save key**, then **Test connection**. The test asks the API whether the configured model is available to the key and uses no tokens.
   - **Assistant settings**: turn on **Enable the research assistant**, then click **Save settings**. The same form sets:
     - the model: `claude-opus-5` by default; `claude-sonnet-5` costs less per token;
     - the daily budget in US dollars (default $2);
     - refusal fallbacks, and the effort level and limits for each feature.
   - **Usage** shows today's estimated spend against the budget and the last seven days of calls.
4. Try it:
   ```bash
   uv run igs assistant status      # enabled, credentials found, spend so far
   uv run igs ask "Which stocks are High conviction, and what keeps the next ones out?"
   uv run igs assistant brief RELIANCE
   uv run igs assistant read-announcements --days 3
   ```
   In the app, use the **Ask** page, or open a stock and click **Write a brief**.

Where the Settings page keeps things:
- **The key** goes into `.env` in the app folder. The file is readable by your user account only, and the page never shows the key in full. A key set as a system environment variable wins over `.env` for the CLI and the scheduled job, so remove that variable if you manage the key on the page.
- **Settings** go into `data/settings/assistant.yaml`. Only values that differ from `config/assistant.yaml` are stored, so `git pull` never conflicts with them. **Reset** deletes the file.
- **Scope.** The CLI, the API and the scheduled job read the same two files, so a change applies everywhere.
- **Local only.** Settings can be changed only while the app is reachable from this computer alone, which is how `igs ui` starts it. Started with `--host 0.0.0.0`, the page is read-only, so nobody else on your network can change the key or the budget.

Without the UI, set the same things by hand: `ANTHROPIC_API_KEY=sk-ant-...` in `.env` (Ubuntu: `nano .env`; Windows: `notepad .env`) and `enabled: true` in `data/settings/assistant.yaml` or `config/assistant.yaml`.

Once it is enabled, the daily job also reads the day's announcements and alerts you to high-materiality ones on your watchlist. Every call's tokens and estimated cost are logged. When the day's estimate reaches the budget, calls stop until the next day (IST). The Anthropic console shows actual charges.

### Updating the app

```bash
git pull
uv sync --all-groups
uv run igs db migrate
uv run igs gate run     # needed after any change to scoring code
```

### Backups

The raw data folder, `data/raw`, is the source of truth: `uv run igs rebuild` rebuilds every table from it. Back up that folder and, for speed of recovery, the database.

Ubuntu:

```bash
pg_dump -Fc -f ~/igs-$(date +%F).dump "postgresql://igs:choose-a-password@localhost:5432/igs"
```

Windows:

```powershell
& "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -Fc -U igs -h localhost -d igs -f "$HOME\igs-backup.dump"
```

To keep the raw data on another drive, set `IGS_RAW_ROOT` in `.env`, for example `IGS_RAW_ROOT=D:\igs-raw`.

---

## Part 5: Troubleshooting

| What you see | What to do |
|---|---|
| `uv` or `git` "not found" right after installing | Open a new terminal or PowerShell window. On Ubuntu, `source $HOME/.local/bin/env`. |
| `password authentication failed for user "igs"` | The password in `.env` doesn't match the one you set in 1.3/2.3. A password with `@ : / ? # %` must be URL-encoded (`@` → `%40`), or changed. |
| `connection refused` on port 5432 | PostgreSQL isn't running. Ubuntu: `sudo systemctl start postgresql`. Windows: start the "postgresql-x64-16" service in Services. |
| `no look-ahead gate record ...; run igs gate run first` or `... code changed since the look-ahead tests last passed` | Run `uv run igs gate run`. This is needed after install and after updates. |
| `<source>: never verified; run igs sources verify <source>` or `latest verification failed` | Run `uv run igs sources verify <source>`. If it keeps failing, NSE is refusing that endpoint from your connection. |
| `schema changed since verification` | NSE changed a file's format. Loading stops so that wrong numbers aren't stored. Re-verify, and check `sources.yaml` before trusting the new format. |
| Long pauses after `403 Forbidden` in the output | NSE's rate limit. The app waits it out once. If a source is refused again right after it answered, wait an hour or until the next day and re-run that command. |
| Everything from NSE refused | Your connection is on NSE's block list (VPN, cloud server). Run from a home or office connection. |
| `UnicodeEncodeError` on Windows | `PYTHONUTF8` isn't set: repeat 2.5 and open a new PowerShell window. |
| `psql` isn't recognised on Windows | Use the full path: `& "C:\Program Files\PostgreSQL\16\bin\psql.exe" ...`. |
| Port 8501 or 8000 already in use | `uv run igs ui --port 8502` or `uv run igs api --port 8001`. |
| Streamlit asks for an email address the first time | Press Enter to skip. |
| The scheduled job didn't run | Ubuntu: `systemctl --user list-timers` and `journalctl --user -u igs-daily.service`. Windows: open Task Scheduler and check "IGS daily" → History, and `logs\daily.log`. |
| The universe is empty | See "Before you start": fewer than 8 quarters of results are loaded. |
| `assistant: the research assistant is off` | Enable it and save an API key on the app's Settings page (Part 4, "The research assistant"). |
| `assistant: ... rejected the credentials` or `no credentials` | The key is missing, mistyped or revoked. Create a new one in the Anthropic console, save it on the Settings page and click Test connection. |
| `assistant: ... reached the daily budget` | Wait until tomorrow (IST) or raise the daily budget on the Settings page. |
| The Settings page says settings can't be changed here | The app was started with `--host 0.0.0.0`. Restart it with plain `uv run igs ui` to make changes. |
| `model ... is not available with these credentials` | Choose another model on the Settings page, or check your API plan in the Anthropic console. |

Everything the app does is recorded: raw responses in `data/raw`, data-quality issues in the database (shown in the UI under Runs), and each job's output in `logs/`.
