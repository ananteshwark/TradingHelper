# IndiaGrowthScreener

> **Personal research tool. Not investment advice. Outputs are screening results from public data and may be wrong or stale. Nothing here is a recommendation to buy, sell or hold any security.**
>
> **Regulatory note.** This tool is built for the author's own research. Sharing its rankings, tiers, reports or alerts with other people, whether free or paid, in a group chat, on social media or through a newsletter, may amount to providing research or recommendations. That can attract obligations under the SEBI (Research Analysts) Regulations, 2014, including registration. Get proper advice before distributing any output.

IndiaGrowthScreener ingests public NSE/BSE data. It computes a transparent multi-factor growth score for Indian listed equities and ranks candidates into tiers (*High conviction / Watchlist / Rejected, with reason*). The evidence behind every number is shown.

It will not place orders, give buy/sell calls or target prices, or predict prices with a black-box model, and v1 depends on no paid data. Broker integrations are read-only; the codebase contains no order-write path, and a test enforces that.

## Build status

| # | Step | Status |
|---|------|--------|
| 1 | Ingestion, storage, instrument master, corporate-action-adjusted prices, reconciliation report | **Foundation done.** Exchange parsers are blocked until endpoints are verified (see below). |
| 2 | XBRL parser with point-in-time columns, validated on 20 hand-checked companies | not started (point-in-time schema and access layer already in place) |
| 3 | Factor library + unit tests | not started (registry and look-ahead harness already in place) |
| 4 | Backtest harness + factor IC report | not started |
| 5 | Scoring + API | not started; **gated** by the look-ahead tests |
| 6 | UI (Streamlit) | not started |
| 7 | Alerts | not started |

### What exists in step 1 so far

- **Raw landing zone** (`igs.ingest.raw_store`). Every payload is stored verbatim with its fetch timestamp, URL and HTTP status. Storage is content-addressed, write-once and read-only on disk. The Postgres index (`raw_payload`) can be rebuilt from disk with `igs raw reindex`, so every table can be rebuilt from raw.
- **Source registry and verification** (`config/sources.yaml`, `igs sources verify`). No endpoint is trusted until a real sample has been fetched, landed and fingerprinted. Ingestion refuses unverified sources. It also stops loudly if a file's columns or JSON keys change.
- **Schema** (`src/igs/db/migrations`). Instrument master with dated ISIN / NSE symbol / BSE code ranges; Postgres exclusion constraints stop one identifier from pointing at two securities at the same time. Unadjusted EOD prices. Corporate actions. Append-only filings and fundamental facts, enforced by triggers. A data-quality issue log.
- **Instrument master builder** (`igs.normalize.instrument_master`). It links symbol renames, and ISIN changes after a face-value split, into one security chain. A symbol reused by an unrelated issuer is never linked.
- **Corporate-action adjustment** (`igs.normalize.adjust`). Split, consolidation, bonus and rights factors. Dividends are used for total return only. Demergers are sent to manual review rather than guessed. Adjusted prices are derived, never stored, and use only actions with `ex_date <= as_of`.
- **Reconciliation checks** (`igs.recon`):
  - no duplicate rows from pre-open or interim sessions;
  - trading-calendar coverage;
  - every ISIN on every day maps to exactly one security;
  - symbol/ISIN consistency;
  - **our corporate-action factors compared with the exchange's own previous-close adjustment**, which catches wrong ratios and missing actions;
  - unexplained price gaps;
  - a close-price cross-check against a second source.

## Point-in-time discipline

- Every fundamental row carries `period_end`, `filed_at` (the exchange dissemination timestamp) and `ingested_at`. All factor math filters on `filed_at <= as_of`, never on `period_end`.
- Restatements are new rows from later filings. Nothing is overwritten: `UPDATE` and `DELETE` on fundamentals are refused by triggers. `fundamental_fact_versioned` numbers the versions, and `facts_as_of(ts)` returns what was public at `ts`.
- Each table has exactly one "known at" rule (`igs.pit.knowledge`): filings when filed, prices at 15:30 IST on the trade date, corporate actions when announced. Signals are formed at end of day T and executed at the next close.
- Factor code reaches data only through `PitView`, and factor modules may not import database, HTTP or raw-store code. A test enforces this.

### The look-ahead gate

`tests/test_lookahead.py` runs every registered factor at a series of dates T three ways:

1. on all the data, checking the view's audit shows nothing later than T;
2. on the data physically cut off at T;
3. on the data with every future value corrupted.

The three outputs must be identical. The suite also proves the harness catches the three classic leaks:

- filtering on `period_end`;
- back-adjusting prices with future splits;
- reading the latest price.

`igs gate run` runs these tests. If they pass, it records a fingerprint of the point-in-time and factor code. The scoring layer calls `require_gate()` and will not run if that code has changed since the tests last passed.

## Data sources

Endpoints are listed in `config/sources.yaml` with their tier.

- **Tier 1:** NSE/BSE archives and APIs.
- **Tier 2:** Angel One SmartAPI, read-only.
- **Tier 3:** Screener.in exports imported by hand (never scraped), and yfinance, flagged as unverified.

Every URL starts as a candidate. `igs sources verify` fetches a sample and lands it in the raw store. It checks that the payload really is the declared format (a blocked request often returns an HTML page with status 200) and records its schema. Sources whose URL is still unknown are listed with `url: null`: the Integrated Filing listing, delisted securities and the Nifty 500 TRI.

**Current blocker.** The cloud environment this was built in cannot reach `nseindia.com`, `bseindia.com` or Angel One (egress policy returns 403), so no endpoint has been verified yet. The exchange parsers (UDiFF and legacy Bhavcopy with session-ID filtering, delivery position, EQUITY_L, corporate actions, surveillance lists) will be written against the real verified payloads, not guessed formats.

## Setup

```bash
uv sync                                   # Python 3.12 + dependencies
createdb igs                              # PostgreSQL 16 (TimescaleDB optional later)
export IGS_DATABASE_URL=postgresql://igs:igs@localhost:5432/igs
uv run igs db migrate
uv run igs sources verify                 # needs network access to NSE/BSE
uv run igs sources list
uv run igs gate run                       # look-ahead tests -> gate record
```

Tests: `uv run pytest`. The database tests need `IGS_TEST_DATABASE_URL` pointing at a disposable database, which is dropped and recreated per test. Without it they are skipped.

Environment variables: `IGS_DATABASE_URL`, `IGS_RAW_ROOT` (default `data/raw`), `IGS_CONFIG_DIR` (default `config/`), `IGS_GATE_PATH`.

## Configuration (decisions so far)

| File | Setting |
|---|---|
| `universe.yaml` | NSE mainboard (EQ, BE), full market cap > ₹500 cr, at least 8 quarters filed as of the screening date. SME and ASM/GSM excluded unless enabled. Buckets use the SEBI method: rank by 6-month average market cap; large = top 100, mid = 101–250, small = 251+. |
| `scoring.yaml` | Pillar weights Growth 35 / Quality 25 / Valuation 15 / Momentum 15 / Ownership 10, equal within each pillar. Winsorised at the 1st/99th percentile. Z-scores within NSE *industry*, falling back to *sector* if an industry has fewer than 8 peers. EV/EBITDA is never applied to banks or NBFCs; config validation rejects it. |
| `backtest.yaml` | Monthly rebalance (primary), with quarterly (quarter end + 60 days) as a sensitivity check. Forward returns at 3, 6 and 12 months, compared with the Nifty 500 TRI. Deciles. Delisted names included. |
| `red_flags.yaml` | Hard filters. Pledge > 20% of promoter holding. Promoter holding down > 5 percentage points over two quarters. Receivable days > 1.5× the 3-year median. At least 2 primary issues adding up to more than 10% of shares in 3 years. ASM/GSM. Contingent liabilities > net worth. Other income > 25% of PBT. If the data needed is missing, the flag reports *data unavailable*; it never counts as a pass. |

## Known limitations (to be addressed, not hidden)

- **Industry classification.** NSE's four-level classification exists only from 2023; earlier backtest periods use today's labels.
- **Historical index membership and ASM/GSM stages.** These have to be rebuilt from exchange circulars and notices; the current files show only today's lists.
- **Annual-report-only data.** Some red-flag inputs, such as contingent liabilities and auditor qualifications, may only appear in annual reports or particular filings. Coverage will be measured, not assumed.

## Layout

```
config/            YAML: universe, scoring, backtest, red flags, sources
src/igs/
  ingest/          raw store, HTTP fetcher (GET only), source verification
  normalize/       instrument master, ISIN rules, corporate-action adjustment
  pit/             point-in-time rules, PitView, look-ahead harness and gate
  recon/           reconciliation checks and report
  factors/         factor registry (library arrives in step 3)
  score/ api/ ui/  later steps
  db/              connection + SQL migrations
tests/
```
