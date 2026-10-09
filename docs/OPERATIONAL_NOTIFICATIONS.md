# Operational Telegram notifications

Set `IGS_OPERATIONAL_ALERTS=1` in the server's private `.env`, with the existing
`IGS_TELEGRAM_TOKEN` and `IGS_TELEGRAM_CHAT_ID`. Run `uv run igs db migrate`, then
`scripts/install-schedules.sh` as the application user. Restart the dashboard so its
error logger sees the setting. `igs-notify.timer` runs once a minute after the previous
notification run finishes. Existing scoring/ingestion schedules are preserved.

- **Data ingestion summary:** once a day, at the first notification check after 21:30
  IST, combining every committed record and correction since the last summary. Prices,
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
- **New quarterly report:** one separate message per successfully loaded current-quarter
  financial filing, for every company, including its name, quarter end and statement
  basis. Comparative-only quarters do not trigger it. Restated filings with different
  content are new events; replaying the same content does not repeat the report alert.
- **Application issues:** data-quality errors, CLI failures, unhandled dashboard
  exceptions (including refreshed fragments), HTTP API failures, failed scheduled services
  and an unavailable dashboard. Runtime messages identify component and error type;
  raw exception strings, credentials and tracebacks stay out of Telegram. Identical runtime
  failures are limited to one notification per hour. DQ errors are grouped by category.
  Warnings and informational findings remain visible in Data quality without Telegram alerts.

Only the ingestion summary waits for its daily time. New issues are dispatched on the next notification
check (normally within about a minute), with runtime/service failures checked first.
Quarterly-report messages, intraday calls and other existing alerts keep their own cadence.
The one-minute dispatcher is not slowed to a daily timer.

Data events share the ingestion transaction and are stored in
`operational_notification`. Delivery failure retains them with increasing retry delays,
up to an hour; there is no attempt cap that silently drops notifications. Runtime errors
use `data/notifications/errors.sqlite3` so database outages can still be reported.
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
