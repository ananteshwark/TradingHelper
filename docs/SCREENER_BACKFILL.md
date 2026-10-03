# Background quarterly-history exports

`igs-screener.timer` runs `igs screener backfill --limit 10` every 30 minutes,
independently of the dashboard. It uses the account owner's authenticated **Export
to Excel** form. It never scrapes financial tables from company-page HTML or bypasses
login, challenges, paywalls or account download limits.

The queue includes companies with fewer than **8 distinct quarterly periods** in
exchange facts and fewer than 8 usable quarters in their latest imported export.
The target is explicitly 8, independent of the configurable minimum used by scoring.
NSE symbols and BSE-only companies are supported. Companies without either identifier
need instrument-master mapping first. This is history coverage, not a guarantee of
8 consecutive recent quarters. New listings can legitimately have fewer than 8.

Priority is recomputed for every batch:

1. Latest AI call is buy/sell, or the latest call from any broker in the last 90 days
   is buy/sell. A newer hold call supersedes the same AI/broker's older recommendation.
2. Highest most recently available composite score, including within the call group.
3. Stable company ID order for ties and stocks without a score.

Requests are spaced at least 10 seconds apart. The worker checks the exchange code
on the company page and the company identity inside the workbook before importing.
It stores the original workbook in the raw store with HTTP provenance, imports it as
Screener enrichment, and records progress in `screener_download`. Existing identical
exports are not re-imported. Successful coverage removes the company from the queue.
Partial exports retry after 30 days; errors back off from 1 hour up to 7 days. A lock
prevents two workers running simultaneously. A crash resumes from committed progress.

Screener exports supplement AI inputs and comparison screens. They **do not** become
exchange filing facts or change the scoring quarter count: the workbook lacks original
publication timestamps and may contain restated figures. Ingestion summaries use the
existing operational Telegram queue when notifications are enabled.

Credentials are saved only in the private server `.env`, never in Git, raw payloads or
logs. Cookies live only in the worker's memory. Configure or correct credentials using:

```bash
cd ~/TradingHelper
uv run igs db migrate
uv run igs screener configure   # password prompt is hidden; validates before saving
scripts/install-schedules.sh
uv run igs screener queue --limit 25
```

A login failure pauses all downloads until `configure` succeeds. HTTP 403/429 or a
non-Excel export response pauses them for 24 hours and reports an operational issue.
No repeated login guesses or rate-limit workarounds are attempted. A manual bounded
batch is `uv run igs screener backfill --limit 1`. Logs are in `logs/screener.log`.
To pause the scheduler: `systemctl --user stop igs-screener.timer`; to resume:
`systemctl --user start igs-screener.timer`.
