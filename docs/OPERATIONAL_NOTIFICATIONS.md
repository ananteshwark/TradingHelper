# Operational Telegram notifications

Set `IGS_OPERATIONAL_ALERTS=1` in the server's private `.env`, with the existing
`IGS_TELEGRAM_TOKEN` and `IGS_TELEGRAM_CHAT_ID`. They go to that chat, except order problems,
which go to the trading chat if one is set (`IGS_TELEGRAM_TRADING_CHAT_ID`, docs/DEPLOY.md).
Run `uv run igs db migrate`, then
`scripts/install-schedules.sh` as the application user. Restart the dashboard so its
error logger sees the setting. `igs-notify.timer` runs once a minute after the previous
notification run finishes. Existing scoring/ingestion schedules are preserved.

## What is sent

`operations` in `config/alerts.yaml` (Settings > Alerts) switches each kind on or off. On
17 October 2026 the owner asked for error alerts only when the app is down or the
database has a problem, not when data wasn't processed. The daily ingestion summary
stays. As shipped:

| Kind | Setting | As shipped |
|---|---|---|
| The app is down | `app_down` | on |
| A database problem | `database` | on |
| A real-money order problem | `orders` | on |
| A failed data job or a data-quality error | `data_problems` | off |
| The daily data ingestion summary | `ingestion_summary` | on |
| Repeated failed sign-ins to the dashboard | `sign_ins` | on |

- **The app is down:** each minute the dispatcher asks the dashboard for its health
  check. One that hasn't answered for 3 minutes gets **APPLICATION DOWN**, which repeats
  every 6 hours while it stays down. **APPLICATION BACK UP**, with how long it was down,
  follows once it answers again. A restart shorter than 3 minutes sends nothing.
  Deployments stop this timer with the others, so they don't trigger it. A deployment
  that fails leaves the app and its timers stopped, and says so in its own output.
- **A database problem:**
  - **Each minute:** PostgreSQL must accept a connection and a query and have every
    migration applied. **DATABASE PROBLEM** and **DATABASE OK AGAIN** follow the same
    timing as the app check.
  - **From any process:** a database error that means trouble with the database itself is
    sent at once, at most hourly per component: a full disk, no memory, too many
    connections, an I/O error, corruption, a read-only or shutting-down server, or a
    missing table or column. A refused connection is left to the minute-by-minute check,
    which waits out restarts.
- **A real-money order problem:** an intraday order rejected or uncertain, a square-off
  or cancellation that failed, an exit left unprotected, an approval that couldn't be
  delivered.
- **Repeated failed sign-ins:** with password sign-in, **SIGN-IN FAILURES** when one
  account, or one address, has had 3 failed sign-ins in 15 minutes.
  - **What it gives:** the count, the account and the addresses tried from; for an
    address, how many tries hit usernames without an account. A username that has no
    account is never named: it may be a password typed in the wrong box.
  - **When it says more:** whether the account is now blocked, and the command to change
    its password (sessions end when it changes).
  - **How often:** at most one message an hour for each account or address. It is
    queued when the failure happens, so it is sent within a minute.
  - **The address:** the visitor's, from Caddy's `X-Forwarded-For`, which the dashboard's
    server trusts from 127.0.0.1 only.
- **Off as shipped, the pages show these instead:**
  - a failed data job (sync, news, screener, ownership, models, the daily job and its
    scheduled services), on Sync and the job logs;
  - a data-quality error, on Data quality.

Messages name the component and the error type only. Raw exception text, credentials
and tracebacks stay out of Telegram.

Not affected by `operations`, with their own settings: AI buy and sell calls, ranking
alerts, intraday calls and approvals, the Plan's and momentum's monthly orders, and
positive quarterly results.

## The daily summary

- **Data ingestion summary** (`ingestion_summary`, on): once a day, at the first notification
  check after 21:30 IST, combining every committed record and correction since the last
  summary. If it is turned off, pending ingestion events are marked skipped, so turning
  it back on starts from new data. Prices,
  delivery updates, listings, facts, ownership, announcements, insider trades, news and
  broker calls are included.
  - It starts with the number of unique companies with new or updated data. Each dataset
    then shows its records and, where its rows identify a company, its companies in
    brackets.
  - Companies are matched from each row's company, security, ISIN or NSE symbol (migration
    053). Securities not yet in the company master, and data that belongs to no company
    (index prices, holidays, news), aren't counted as companies.
  - Duplicate or no-op loads and rolled-back transactions don't create messages.
  - The last summary's time is stored in PostgreSQL, so a restart doesn't repeat or skip a
    day. A server that was off at 21:30 sends the summary on its next check.
  - A failed send keeps the whole summary and retries after five minutes.
## Quarterly results

- **Quarterly results read positive:** each successfully loaded current-quarter financial
  filing, for every company, is read from its figures against the same quarter a year
  earlier (`igs.results_read`), and only a report that reads **positive** is sent: revenue
  up at least 5%, profit up at least 15% (or a loss turned into a profit), and no surprise
  below +1 standard deviation where eight or more quarters show the usual profit change.
  The message gives the figures, the read, and how earlier positive reads moved against
  the Nifty 500 the next session. At most one message per company and quarter, whichever
  of its filings (basis, exchange) reads positive first.
  - The thresholds, and `only_positive` (off: every report, with its read), are
    `rules.quarterly_results` in `config/alerts.yaml`, on Settings > Alerts.
  - Every report is read and kept in `results_read`, sent or not; a report not sent is
    marked `skipped` with the reason (`results read mixed`, `already sent for this
    quarter`), not `sent_at`.
  - The rule is not validated. The **Paper tracking** page's **Results days** tab shows each read with the
    stock's move against the Nifty 500 from the last close before the filing to the next
    session and 21 sessions on, so it shows whether positive reads were followed by rises.
  - Comparative-only quarters do not trigger it. Restated filings with different content
    are new events; replaying the same content does not repeat it.
## Data problems, when turned on

`data_problems` sends **APPLICATION ISSUE** for data-quality errors, CLI failures,
unhandled dashboard exceptions (including refreshed fragments), HTTP API failures and
failed scheduled services. Identical runtime failures are limited to one notification
per hour. DQ errors are grouped by category. Warnings and informational findings remain
visible in Data quality without Telegram alerts. While it is off, pending data-quality
events are marked skipped.

## Delivery

Only the ingestion summary waits for its daily time. New issues are dispatched on the next notification
check (normally within about a minute), with runtime/service failures checked first.
Quarterly-results messages, intraday calls and other existing alerts keep their own cadence.
The one-minute dispatcher is not slowed to a daily timer.

Data events share the ingestion transaction and are stored in
`operational_notification`. Delivery failure retains them with increasing retry delays,
up to an hour; there is no attempt cap that silently drops notifications. Runtime errors
use `data/notifications/errors.sqlite3` so database outages can still be reported; the
app-down and database checks keep their state there too.
`uv run igs notify --check-services` performs the same check manually. Telegram delivery
is at-least-once: a crash after Telegram accepts a message but before the local
acknowledgement can repeat it. Notifications start with new activity after migration;
existing historical filings are not announced retroactively.

The monitor runs on this server. It cannot report while the whole server is powered off,
its network is down, or Telegram is unavailable; queued messages retry after recovery.
A separate external uptime monitor is needed for immediate whole-server outage alerts.
User input validation notices are not operational errors and do not trigger alerts.

Scheduled AI work that reaches the daily spending cap is deferred, not a failed
service. Pending articles and broker reviews remain available for a later run.
Manual assessment commands still report that they could not run. Feed, database,
and model API errors remain failures; increasing collection frequency does not
increase the AI budget.

The sync job refreshes the verified exchange holiday calendar before price
collection, so it does not repeatedly request price files for known holidays.
Default source verification reports registry entries without URLs as unconfigured
and skips them. Explicitly verifying one still fails. A configured source returning
HTTP 403 or a missing filing returning HTTP 404 is not marked verified or repaired:
source access or the exchange's document link must be corrected upstream.

When another news or broker collector already owns the database lock, a duplicate
run is deferred without a service failure. The active worker retains responsibility
for reporting actual feed failures.
