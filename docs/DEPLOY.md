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
- **NSE rate limits.** The app waits 5 seconds between requests to NSE. If NSE still answers "403 Access Denied", the app prints "waiting 330 s before one more try", pauses for about 5½ minutes and tries once more. Those pauses are normal, so don't interrupt them. Some NSE pages refuse every automated request (the per-stock quote page does); after one wait, the app skips such a page for the rest of that run and carries on with the others.
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

Three systemd timers:
- **igs-daily**: weekdays at 20:30 IST, after NSE publishes the day's files. It loads them, re-scores and sends alerts.
- **igs-sync**: every 2 hours. It checks NSE for new files (filings, announcements, insider trades, price files) and downloads only what is new. It doesn't re-score.
- **igs-verify**: Saturdays. It re-verifies the sources.

If the computer was off when a job was due, the job runs when it is next on. The app also checks for new files while it is open (see 1.8), so the 2-hourly timer covers the time the app is closed. Only one check runs at a time.

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

cat > ~/.config/systemd/user/igs-sync.service <<'EOF'
[Unit]
Description=IndiaGrowthScreener: check NSE for new files

[Service]
Type=oneshot
ExecStart=%h/TradingHelper/scripts/igs-job.sh sync --trigger timer
EOF

cat > ~/.config/systemd/user/igs-sync.timer <<'EOF'
[Unit]
Description=Check NSE for new files every 2 hours

[Timer]
OnCalendar=0/2:15
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now igs-daily.timer igs-verify.timer igs-sync.timer
sudo loginctl enable-linger "$USER"     # keep the timers running when you are logged out
systemctl --user list-timers            # shows the next run of each
```

To run the job now and watch it:

```bash
systemctl --user start --no-block igs-daily.service
tail -f ~/TradingHelper/logs/daily.log        # Ctrl+C stops watching, not the job
```

Cron works too: run `crontab -e` and paste the lines from `scripts/crontab.example`. Cron doesn't catch up on runs missed while the computer was off.

### 1.8 Open the app

```bash
cd ~/TradingHelper
uv run igs ui           # opens http://localhost:8501 in your browser; Ctrl+C stops it
uv run igs api          # optional, in another terminal: http://localhost:8000/docs
```

Both listen on this computer only. Use `--port` if the default port is taken.

While the app is open, it checks NSE for new files when it starts and every 2 hours after that. The sidebar shows the last check and has a **Check NSE now** button. Each check's output goes to `logs/sync.log`. To turn the checks off, start the app with `uv run igs ui --no-sync`.

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

These commands create three tasks:
- **IGS daily:** weekdays at 20:30, after NSE publishes the day's files. It loads them, re-scores and sends alerts.
- **IGS new files:** every 2 hours. It checks NSE for new files and downloads only what is new. It doesn't re-score. The app also checks while it is open (see 2.8). Only one check runs at a time.
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

$sync = New-ScheduledTaskAction -Execute "$repo\scripts\igs-job.cmd" -Argument "sync --trigger timer" -WorkingDirectory $repo
$every2h = New-ScheduledTaskTrigger -Once -At "00:15" -RepetitionInterval (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "IGS new files" -Action $sync -Trigger $every2h -Settings $settings
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

While the app is open, it checks NSE for new files when it starts and every 2 hours after that. The sidebar shows the last check and has a **Check NSE now** button. Each check's output goes to `logs\sync.log`. To turn the checks off, start the app with `uv run igs ui --no-sync`.

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

**Corporate actions, announcements, insider trades and the instrument master:**

```bash
uv run igs ingest range nse_corporate_actions --start 2014-01-01 --end 2026-12-31
uv run igs ingest range nse_announcements --start 2025-01-01 --end 2026-09-23
uv run igs sources verify nse_insider_trading nse_insider_disclosures
uv run igs ingest range nse_insider_trading --start 2025-01-01 --end 2026-05-02
uv run igs ingest range nse_insider_disclosures --start 2026-04-25 --end 2026-09-23
uv run igs master rebuild
uv run igs ingest documents insider_trading
```

Insider trades come from two NSE sources, because NSE changed systems around May 2026:
- `nse_insider_trading` has one row per trade, for dates up to about 2 May 2026. For later dates it returns an empty list, so it is verified on 1-7 April 2026.
- `nse_insider_disclosures` has one row per disclosure since then, each with a link to an XBRL file holding the trades. It asks NSE for 7 days at a time. `igs ingest documents insider_trading` fetches each file once, in the same way as results and shareholding documents.

A trade listed by both sources around the changeover is loaded once. The 2-hourly check loads new disclosures and their files; the commands above are for history.

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
uv run igs gate run     # again after every update: scoring refuses code the gate hasn't passed
uv run igs score
```

`igs score` prints how many companies made the universe and why the others were left out. The app's Rankings page shows the same, with a list of what is loaded and what is still missing. Until a score run exists, the page says "No score run yet" and shows only that list.

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

Between daily runs, new files are checked for:
- by the app, when it starts and every 2 hours while it is open;
- by the 2-hourly scheduled task, when the app is closed.

Each check:
- asks only for what isn't loaded yet:
  - price files for days not in the database;
  - listing pages, until one has nothing new;
  - insider-trading disclosures from the day of the last one loaded (up to 90 days back), so a computer that was off for a while leaves no gap;
  - documents not fetched before, up to 500 per check of each kind (results, shareholding, insider trades);
  - the Economic Times stock-news feeds for brokers' calls (`config/broker_calls.yaml`); with the assistant on, the AI reads the articles that mention a rating or target;
- asks for today's price files only after 19:00 IST, when NSE has published them;
- is recorded in the database, shown in the app's sidebar and logged to `logs/sync.log`;
- doesn't re-score. New data reaches the rankings at the next daily run, or when you run `uv run igs score`.

To check now: `uv run igs sync`.

A check won't start within an hour of the previous one, so reopening the app doesn't send NSE the same requests again. `uv run igs sync --force` skips that wait. Only one check runs at a time.

`config/sync.yaml` sets:
- the interval;
- the minimum gap;
- the document limit per check.

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

### Telegram: the digest and a message for each AI buy or sell call

Telegram and its bot service are free. It gets the daily digest. It also gets a detailed message for each new buy or sell call by the AI: a stock's first buy or sell, or a change to buy or sell (`call_messages` in `config/alerts.yaml`). The message has the call, confidence and horizon, the last close the AI saw, its summary, reasons, when to buy, when to sell, risks, what prompted it, and how the AI's earlier calls turned out. Messages go out when the daily job finishes; a failed one is kept and retried on the next run.

1. Install Telegram on your phone and sign in.
2. In Telegram, search for **@BotFather** (the official one has a blue tick) and send `/newbot`. Give your bot a name, then a username ending in `bot` (for example `ananth_igs_bot`). BotFather replies with a token like `123456789:AAE...`.
3. Open the app's **Settings** page, **Telegram alerts**, and paste the token into **Bot token**.
4. In Telegram, open your new bot (BotFather's reply links to it) and press **Start**.
5. Back on the Settings page, click **Find my chat ID**. It fills in your chat ID from the message you just sent. Click **Save Telegram settings**, then **Send a test message**.

`uv run igs alerts --test-telegram` sends the same test from a terminal. Only you can find your bot unless you share its username, and it only writes to the chat ID saved in `.env`.

### WhatsApp messages for the AI's buy and sell calls

Each new buy or sell call by the AI (a stock's first buy or sell, or a change to buy or sell) comes to WhatsApp as a detailed message, the same as on Telegram. The message covers the call, confidence and horizon, the last close the AI saw, its summary, reasons, when to buy, when to sell, risks, what prompted it, and how the AI's earlier calls turned out. Holds and repeated calls are not sent (`call_messages` in `config/alerts.yaml`). The messages go out when the daily job finishes. A message that fails is kept and retried on the next run.

Two services can send them. Set either one up on the app's **Settings** page, under **WhatsApp alerts**. It saves the keys in `.env` and has a **Send a test message** button. `uv run igs alerts --test-whatsapp` does the same test from a terminal. You can switch services at any time.

**CallMeBot (free, 2 minutes).** A free third-party service for personal use. Messages pass through its servers, and it gives no guarantee: if it stops working, switch to Meta.

1. Save CallMeBot's number as a contact on your phone. On 30 September 2026 it was **+34 684 783 347**. It changes from time to time; www.callmebot.com, "Free WhatsApp API", has the current one.
2. From the WhatsApp account that should get the alerts, send that contact exactly `I allow callmebot to send me messages`. It replies "API Activated for your phone number. Your APIKEY is ...".
3. If no reply comes within 2 minutes, CallMeBot asks you to try again after 24 hours: its free bot is often busy. If it still doesn't answer, use Meta instead.
4. On the Settings page, choose **CallMeBot**, enter your number with its country code (`+919812345678`) and the API key. Click **Save WhatsApp settings**, then **Send a test message**.

**WhatsApp Cloud API (Meta, official, about 30 minutes once).** Meta charges per message delivered, about ₹0.15 at India's rate for utility messages in 2026. It sends only messages laid out by a template it has approved, so each call fits in about 1,000 characters and long parts are shortened.

1. At developers.facebook.com, log in with Facebook and create an app with the use case **Connect with customers through WhatsApp**. Meta creates a WhatsApp Business account and a free test number for it.
2. In the app, open **WhatsApp**, **API Setup**. Under **To**, add your own WhatsApp number; Meta sends it a code to confirm. Copy the **Phone number ID** shown under the **From** number. It is a long number, not the phone number itself.
3. Create a permanent access token (the one on API Setup expires within a day):
   1. In **Business settings** (business.facebook.com), open **System users** and click **Add**. Give it a name and the Admin role.
   2. Click the new user, then **Assign assets**. Choose your app with **Manage app**, and your WhatsApp account with **Manage WhatsApp Business accounts**.
   3. Click **Generate token**. Choose never to expire, and tick `business_management`, `whatsapp_business_messaging` and `whatsapp_business_management`.
4. Create the message template in **WhatsApp Manager**, **Message templates**, **Create template**:
   - Category **Utility**, name `igs_ai_call`, language **English**.
   - Variables of type **Number**.
   - The body below, word for word.

   Meta asks for a sample of each variable. Use for example: `BUY Example Ltd (EXAMPLE), was hold` · `63%, 12 months` · `Rs 845.20 on 29 Nov 2026` · `Margins and momentum improved.` · `ROE 18%; revenue up 20%` · `Close above the 200-day average` · `Operating margin below 12%` · `Demand could slow` · `new results filed` · `no earlier buy call is a month old yet`.

   Approval takes minutes to a day. Meta may file the template as Marketing instead, which costs more per message.

```
New AI call from IndiaGrowthScreener: {{1}}

Confidence and horizon: {{2}}
Last close it saw: {{3}}

Summary: {{4}}

Reasons: {{5}}

Buy when: {{6}}

Sell when: {{7}}

Risks: {{8}}

Prompted by: {{9}}

Record so far: {{10}}

This is a language model's judgement, checked only by its own record. It is not investment advice; the decision and its risk are yours.
```

5. On the Settings page, choose **WhatsApp Cloud API (Meta)**. Enter your number with its country code, the access token and the phone number ID. Click **Save WhatsApp settings**, then **Send a test message**.

### The research assistant (optional, AI)

The assistant answers questions about a run, writes plain-language briefs of a stock's result and reads new announcements. When you ask, it also makes buy / hold / sell calls on single stocks, with when to buy and when to sell (README, "AI buy / hold / sell calls"). It uses the Claude API, which is billed per use, and is off until you turn it on. Those features do not affect rankings. The explicit [geopolitical news feature](GEOPOLITICAL_NEWS.md) can affect ratings through stored, time-stamped assessments and a capped adjustment. The README section "Research assistant" describes the data sent to the API.

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
   uv run igs assistant call RELIANCE   # the AI's call, with when to buy and when to sell
   uv run igs assistant calls           # past calls and how they did against the Nifty 500
   ```
   In the app, use the **Ask** page, or open a stock and click **Write a brief** or **Ask the AI for a call**. The **AI calls** page lists every call and its record.

Where the Settings page keeps things:
- **The key** goes into `.env` in the app folder. The file is readable by your user account only, and the page never shows the key in full. A key set as a system environment variable wins over `.env` for the CLI and the scheduled job, so remove that variable if you manage the key on the page.
- **Settings** go into `data/settings/assistant.yaml`. Only values that differ from `config/assistant.yaml` are stored, so `git pull` never conflicts with them. **Reset** deletes the file.
- **Scope.** The CLI, the API and the scheduled job read the same two files, so a change applies everywhere.
- **Local only.** Settings can be changed only while the app is reachable from this computer alone, which is how `igs ui` starts it. Started with `--host 0.0.0.0`, the page is read-only, so nobody else on your network can change the key or the budget.

Without the UI, set the same things by hand: `ANTHROPIC_API_KEY=sk-ant-...` in `.env` (Ubuntu: `nano .env`; Windows: `notepad .env`) and `enabled: true` in `data/settings/assistant.yaml` or `config/assistant.yaml`.

Once it is enabled, the daily job also:
- reads the day's announcements and alerts you to high-materiality ones on your watchlist;
- reads brokers' buy, hold and sell calls out of Moneycontrol's and the Economic Times' stock news (every check does this too; README, "Brokers' calls"). Moneycontrol's news list covers about two days; for older calls, open moneycontrol.com/news/business/stocks in your browser, press Ctrl+A and Ctrl+C, and paste it on the **AI calls** page under "Import older brokers' calls from Moneycontrol". A single call can also be added on a stock's page;
- makes AI calls automatically, at most 10 a day (Settings, "AI buy / hold / sell calls"). It covers your watchlist, the 20 best-ranked stocks, stocks with a broker's call in the last 7 days, and stocks whose latest call is buy or hold. A stock gets a new call when new results, shareholding, insider trades, a material announcement or a broker's call arrived since its last call, when its tier changed or a red flag tripped, or when its last call is more than 7 days old. Each call says how it compares with the brokers' calls;
- alerts you to a stock's first call, and to any change of call.

Automatic calls need the daily job to run (1.7 or 2.7). The **AI calls** page shows which stocks are due and has a button to make those calls now. `uv run igs assistant auto-calls` does the same from the command line, and `--dry-run` only lists them.

Each AI call costs roughly US$0.10-0.30. With 10 automatic calls a day, raise the daily spending threshold to about US$5, or lower the calls a day. Every request's tokens and estimated cost are logged. When the day's estimate reaches the budget, requests stop until the next day (IST). The Anthropic console shows actual charges.

### Updating the app

Stop the app first (Ctrl+C where `uv run igs ui` runs), then:

```bash
git pull
uv sync --all-groups
uv run igs db migrate
uv run igs gate run     # needed after any change to scoring code
uv run igs ui
```

An app left running during `git pull` keeps parts of the old code loaded, and pages can fail with errors such as "Extra inputs are not permitted" in Settings. It shows a red "updated while it was running" banner until it is restarted. `uv run igs ui` also applies any new database migrations when it starts.

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
| `connection refused` on port 5432 | PostgreSQL isn't running. Ubuntu: `sudo systemctl start postgresql`. Windows: start the "postgresql-x64-16" service in Services. |
| `no look-ahead gate record ...; run igs gate run first` or `... code changed since the look-ahead tests last passed` | Run `uv run igs gate run`. This is needed after install and after updates. |
| `<source>: never verified; run igs sources verify <source>` or `latest verification failed` | Run `uv run igs sources verify <source>`. If it keeps failing, NSE is refusing that endpoint from your connection. |
| `schema changed since verification` | NSE changed a file's format. Loading stops so that wrong numbers aren't stored. Re-verify, and check `sources.yaml` before trusting the new format. |
| Long pauses after `403 Forbidden` in the output | NSE's rate limit. The app waits it out once. If a source is refused again right after it answered, wait an hour or until the next day and re-run that command. |
| Everything from NSE refused | Your connection is on NSE's block list (VPN, cloud server). Run from a home or office connection. |
| `UnicodeEncodeError` on Windows | `PYTHONUTF8` isn't set: repeat 2.5 and open a new PowerShell window. |
| `psql` isn't recognised on Windows | Use the full path: `& "C:\Program Files\PostgreSQL\16\bin\psql.exe" ...`. |
| Settings says `features.call Extra inputs are not permitted` (or another setting "not permitted") | The app was updated (`git pull`) while it was running and still has the old code loaded. Stop it (Ctrl+C) and run `uv run igs ui` again. Don't reset the settings; that doesn't fix it. |
| `password authentication failed for user "igs"` | The password in `IGS_DATABASE_URL` does not match the database role's, the role was never created, or a shell variable `IGS_DATABASE_URL` overrides `.env`. `igs` prints which URL it used (password hidden) and where it came from. Set one password in both places: `sudo -u postgres psql -c "ALTER ROLE igs WITH LOGIN PASSWORD 'MyPass2026';"` (Windows: `psql -U postgres -c ...`) and `IGS_DATABASE_URL=postgresql://igs:MyPass2026@localhost:5432/igs` in `.env`. Avoid `@ : / ? # %` in the password. |
| `database "igs" does not exist` | `sudo -u postgres createdb -O igs igs` (Windows: `createdb -U postgres -O igs igs`). |
| Port 8501 or 8000 already in use | `uv run igs ui --port 8502` or `uv run igs api --port 8001`. |
| Streamlit asks for an email address the first time | Press Enter to skip. |
| Sidebar says `failed: announcements, insider trades, ...` | Those NSE pages refused your connection, and `logs/sync.log` shows the reason for each. "Not asked again" means that page was still refused after a 5.5-minute wait, so the check skipped it; other pages were still asked. After three refusals in a row on one site, the check skips the rest of that site. The next check tries again. Prices and delivery files come from a different NSE site and usually still load. |
| `skipped (the last check started ... min ago ...)` | A check ran recently. Wait, or run `uv run igs sync --force`. |
| Sidebar: `The check started ... did not finish` | The computer was switched off or the process was stopped during a check. Nothing is needed: the next check marks that one interrupted and picks up where the data stops. |
| `The database is missing a table (relation "sync_run" does not exist)` | The app was updated but the database wasn't. Run `uv run igs db migrate`. |
| `filing_unmapped: SYMBOL: no company in instrument master` for many filings | The instrument master was empty when those documents were read, so none was stored. They have been downloaded, so they aren't fetched again. Run `uv run igs master rebuild` (it should print more than 0 securities), then `uv run igs rebuild`. The rebuild re-reads everything already downloaded without asking NSE; it takes a few minutes. If an NSE check or another load is running, it says so and stops; run it again afterwards. |
| `Stopped: N ... documents are waiting, but the instrument master is empty` | Load prices and run `uv run igs master rebuild` first (3.2), then load the documents. |
| The **Check NSE now** button is greyed out | A check is running, or the last one started less than an hour ago. The caption above the button says which. |
| The scheduled job didn't run | Ubuntu: `systemctl --user list-timers` and `journalctl --user -u igs-daily.service`. Windows: open Task Scheduler and check "IGS daily" → History, and `logs\daily.log`. |
| "No score run yet" on the Rankings page | `igs score` hasn't completed. The page lists what is missing. After an update it is usually the look-ahead gate: run `uv run igs gate run`, then `uv run igs score`, and reload the page. |
| The universe is empty, or "0 companies" | The warning on the Rankings page and the output of `igs score` give the reason. Usually fewer than 8 quarters of results are loaded: see "Before you start" and 3.4. If no results filings are listed at all, NSE is refusing `www.nseindia.com` from your connection. |
| `assistant: the research assistant is off` | Enable it and save an API key on the app's Settings page (Part 4, "The research assistant"). |
| `assistant: ... rejected the credentials` or `no credentials` | The key is missing, mistyped or revoked. Create a new one in the Anthropic console, save it on the Settings page and click Test connection. |
| `assistant: ... reached the daily budget` | Wait until tomorrow (IST) or raise the daily budget on the Settings page. |
| The Settings page says settings can't be changed here | The app was started with `--host 0.0.0.0`. Restart it with plain `uv run igs ui` to make changes. |
| `model ... is not available with these credentials` | Choose another model on the Settings page, or check your API plan in the Anthropic console. |
| `WhatsAppError: The WhatsApp Cloud API refused the message: Invalid OAuth access token ...` | The token is wrong, or it is the temporary one from API Setup. Create a permanent token (Part 4, "WhatsApp messages", step 3) and save it on the Settings page. |
| `... Template name does not exist ...` or a template that is not approved | The `igs_ai_call` template is missing, still in review, or in another language. Check WhatsApp Manager, Message templates; its name and language must match `config/alerts.yaml`. |
| `... Recipient phone number not in allowed list` | Add your number under **To** on the app's API Setup page and confirm the code Meta sends. |
| `WhatsAppError: CallMeBot refused the message: APIKey is invalid` | Send CallMeBot the permission message again for a new key, and save it on the Settings page. |
| `TelegramError: Telegram refused the request: Unauthorized` | The bot token is wrong or was revoked. Copy it again from @BotFather (`/mybots`, your bot, API Token) and save it on the Settings page. |
| `Telegram refused the request: Bad Request: chat not found` or `Forbidden: bot was blocked by the user` | Open your bot in Telegram and press Start (or unblock it), then click **Find my chat ID** and save again. |
| **Find my chat ID** says there are no messages | Send your bot any message in Telegram first. Telegram keeps them for about a day, so do it just before clicking. |
| The stock page's key numbers show n/a for P/E, EPS, book value and dividend yield, with a note | They are stored with each scoring run from this version on. Run `uv run igs score` (or wait for the daily job). |
| `broker calls` failed: `Economic Times - Stocks: no new items since ...; the feed may have stopped` (or the same for a news feed) | The publisher stopped updating that RSS feed, as Moneycontrol did in April 2024. Find its current feed on the publisher's RSS page and replace the URL in `config/broker_calls.yaml` (or `config/news.yaml`), or remove it. |
| The AI calls page says news articles are waiting to be read | The assistant is off or over its daily budget. Turn it on (Settings) or wait for the next day; articles older than 7 days are no longer read. |
| A newly listed company isn't in the stock search | The search lists a company once NSE's equity list or its first day's price file (after 19:00 IST) is loaded. The dates under the search box say what is loaded. Run `uv run igs sync --force` to check now. Until NSE's equity list has it, it is named from the price file in short form (for example "Manipal Payment & Ide S L"), so search a shorter part of the name or its symbol. It enters the ranking after 8 quarters of results; until then its page shows its prices, filings and brokers' calls. |
| Pasting Moneycontrol's page finds no headlines | Copy the whole stock news page, moneycontrol.com/news/business/stocks (Ctrl+A, then Ctrl+C), rather than a single article, and paste it again. Only headlines that state a call ("Buy X; target of Rs N: Broker") are read. If Moneycontrol changed its headline format, add the calls one by one on the stock page. |
| `broker calls` failed: `Moneycontrol - stock and market news: HTTPStatusError: feed fetch or parse failed` | Moneycontrol refused or failed the request for its news sitemap. The app doesn't retry with a disguised client: calls from the Economic Times still arrive. If it keeps failing, remove the Moneycontrol entry from `config/broker_calls.yaml` and paste its stock news page now and then instead. |
| A broker's call shows "(not matched)" | The article names a company the instrument master doesn't have under that name, or a name several companies share. Add the call yourself on the right stock's page if it matters. |
| CallMeBot never sends the API key | Check you messaged its current number (www.callmebot.com, "Free WhatsApp API") with the exact text `I allow callmebot to send me messages`, from the WhatsApp account that should get the alerts. With no reply in 2 minutes, try again after 24 hours, as CallMeBot asks. If it still doesn't answer, set up Meta's Cloud API instead. |
| No WhatsApp or Telegram message after a buy or sell call | Only a stock's first buy or sell and changes to buy or sell get a message, when the daily job finishes; calls you make in the app arrive after the next daily job. Check `logs/daily.log` for an `alerts` error, and try **Send a test message** on the Settings page. |

Everything the app does is recorded: raw responses in `data/raw`, data-quality issues in the database (shown in the UI under Runs), and each job's output in `logs/`.
