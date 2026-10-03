# Operational Telegram notifications

Set `IGS_OPERATIONAL_ALERTS=1` in the server's private `.env`, with the existing
`IGS_TELEGRAM_TOKEN` and `IGS_TELEGRAM_CHAT_ID`. Run `uv run igs db migrate`, then
`scripts/install-schedules.sh` as the application user. Restart the dashboard so its
error logger sees the setting. `igs-notify.timer` runs once a minute after the previous
notification run finishes. Existing scoring/ingestion schedules are preserved.

- **Data ingestion summary:** committed new records and corrections, grouped by dataset
  per dispatch batch. Prices, delivery updates, exchange listings, filings, facts,
  ownership, announcements, insider trades, news and broker calls are included.
  Counts are records, not unique companies or downloaded files. Duplicate/no-op loads
  and rolled-back transactions do not create messages. Large backfills can span batches.
- **New quarterly report:** one separate message per successfully loaded current-quarter
  financial filing, for every company, including its name, quarter end and statement
  basis. Comparative-only quarters do not trigger it. Restated filings with different
  content are new events; replaying the same content does not repeat the report alert.
- **Application issues:** data-quality warnings/errors, CLI failures, unhandled dashboard
  exceptions (including refreshed fragments), HTTP API failures, failed scheduled services
  and an unavailable dashboard. Runtime messages identify component and error type;
  raw exception strings, credentials and tracebacks stay out of Telegram. Identical runtime
  failures are limited to one notification per hour. DQ issues are grouped by category.

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
