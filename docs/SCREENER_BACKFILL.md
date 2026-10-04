# Background financial-history exports

`igs-screener.timer` runs `igs screener backfill --limit 10` every 30 minutes,
independently of the dashboard. It uses the account owner's authenticated **Export
to Excel** form. It never scrapes financial tables from company-page HTML or bypasses
login, challenges, paywalls or account download limits.

The queue covers companies with an NSE symbol or BSE code, including those that already
have eight exchange quarters. Verified exports refresh every 30 days, filling supported
quarterly, annual, balance-sheet and cash-flow gaps. New listings can legitimately have
short histories; missing observations remain missing.

Priority is recomputed for every batch:

1. Latest AI call is buy/sell, or the latest call from any broker in the last 90 days
   is buy/sell. A newer hold call supersedes the same AI/broker's older recommendation.
2. Highest most recently available composite score, including within the call group.
3. Stable company ID order for ties and stocks without a score.

Requests are spaced at least 10 seconds apart. The worker checks the exchange code
on the company page and the company identity inside the workbook before importing.
It stores the original workbook in the raw store with HTTP provenance, imports it as
Screener enrichment, and records progress in `screener_download`. Existing identical
exports are not re-imported. Successful downloads defer the next refresh for 30 days.
Partial exports retry after 30 days; errors back off from 1 hour up to 7 days. A lock
prevents two workers running simultaneously. A crash resumes from committed progress.

Verified background exports also fill missing quarterly inputs in scoring and the
minimum-quarter eligibility check. Sales, net profit, PBT, other income, financing,
depreciation and operating profit are mapped explicitly from crore to INR. Exchange
values take precedence, and the company's exchange reporting basis is preserved.
The workbook's import time and reporting-basis verification time bound its availability:
an export downloaded today cannot affect yesterday's score or backtest. Export versions
remain separate from exchange filing records. Factor details identify each source export,
period, basis and observation time; score explanations identify Screener supplementation.
March year-end annual statements supply sales, profits, financing, depreciation and
operating cash flow. Interim/non-March duration columns, ambiguous fields and manual
exports without verified basis remain AI enrichment. Balance-sheet totals are admitted
only when both sides reconcile. Borrowings and cash/bank aggregates retain separate
concepts; no current/noncurrent split is invented. Matched annual endpoints can fill
3/5-year CAGR gaps and the growth input to PEG, explicitly labelled in factor detail;
they never manufacture quarters. Existing exports are reverified once for scoring eligibility. Operational
Telegram ingestion summaries continue to use the existing notification queue.

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


## Historical shareholding and remaining sources

`igs-ownership.timer` runs hourly at :22, discovers 10 prioritized companies' NSE
per-symbol shareholding archives and loads up to 100 pending filings from the last two
years, newest first. Listings are checked weekly per company. HTTP failures back off
for a day. Raw listings and XML are preserved with exchange broadcast timestamps.
Manual run: `uv run igs ownership-backfill --limit 10 --documents 100`.

NSE's [per-company shareholding archive](https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern)
returned 22 Reliance filings (September 2021–June 2026) during verification on 2026-10-04.
The existing master feed only exposed the latest quarter. The archive supplies promoter,
FII/DII, holder-count and pledge history where disclosed. Fractional ownership units are
converted using the filing's total row; absent categories are not assumed zero.

[BSE's shareholding search](https://www.bseindia.com/corporates/Sharehold_Searchnew.aspx)
is an alternative source, but its public API returned HTTP 403 from this server during
verification. It is not presented as a working automatic fallback. BSE-only ownership
history remains a gap until a permitted feed or issuer filing can be validated.

Prices/volume/delivery continue to use exchange bhavcopies. Sector classification still
needs a working quote/classification feed; financial exports do not supply it. Long
quarterly growth consistency, historical valuation, sector-specific disclosures and
undisclosed pledge figures cannot be fabricated from annual statements. The audit and
factor statuses continue to expose these gaps; 100% coverage is not guaranteed.

Legacy ownership XML can contain company metadata pointing to absent contexts. Ownership
parsing selects only its configured share/percentage/pledge/holder measures and validates
every context those measures reference; it never invents dates or contexts. Cached rejected
files can be replayed after parser updates without re-downloading.
