# IndiaGrowthScreener

> **Personal research tool. Not investment advice. Outputs are screening results from public data and may be wrong or stale. Nothing here is a recommendation to buy, sell or hold any security.**
>
> **Regulatory note.** This tool is built for the author's own research. Sharing its rankings, tiers, reports or alerts with other people, whether free or paid, in a group chat, on social media or through a newsletter, may amount to providing research or recommendations. That can attract obligations under the SEBI (Research Analysts) Regulations, 2014, including registration. Get proper advice before distributing any output.

IndiaGrowthScreener ingests public NSE/BSE data. It computes a transparent multi-factor growth score for Indian listed equities, point in time, within industry peer groups. It sorts the universe into tiers: *High conviction*, *Watchlist*, *Not shortlisted* and *Rejected, with reason*. Every number traces back to the filing row it came from.

It does not place orders, give buy/sell calls or target prices, or use black-box ML, and v1 depends on no paid data. Broker access is read-only, and a test fails if an order endpoint ever appears in the code.

## Status

All seven build steps are implemented and tested (268 tests; CI runs lint, the look-ahead gate and the full suite against PostgreSQL 16). **None of it has run on real exchange data yet**: the cloud environment used for development could not reach NSE, BSE or Angel One. Parsers follow the exchanges' documented formats and sit behind a verification gate that refuses to ingest from an unverified endpoint. The work that remains is validation, and it needs real data:

| # | Step | Built | Still to do with real data |
|---|---|---|---|
| 1 | Ingestion, instrument master, adjusted prices, reconciliation report | yes | `igs sources verify`, backfill, run `igs recon` and review the report |
| 2 | XBRL parser (2022 and 2024 taxonomies, both NSE filing systems), shareholding, 20-company validation | yes | Confirm element names against real instances (the mapping lives in YAML); type hand-checked values into `config/hand_checked.yaml`; run `igs validate fundamentals` |
| 3 | Factor library (32 factors) and unit tests | yes | - |
| 4 | Walk-forward backtest, costs, factor IC report | yes | Run on 10+ years of data; drop factors the IC gate rejects |
| 5 | Scoring and API | yes | Tune the tier thresholds once real IC results exist |
| 6 | UI (Streamlit) | yes | - |
| 7 | Alerts (email, Telegram) and daily job | yes | Set the channel credentials; install the cron entry |

Sources whose URL is still unknown are `url: null` in `config/sources.yaml`: the Integrated Filing listing, delisted securities and the Nifty 500 TRI. Until the TRI is found, the backtest benchmarks against the Nifty 500 price index and says so loudly in every report.

## How it works

```
raw landing zone (immutable) -> normalize -> point-in-time view -> factors -> score -> API / UI / alerts
        ^ igs sources verify                  ^ look-ahead gate             ^ backtest IC gate
```

- **Ingestion (`igs.ingest`).** Every response is stored verbatim, with its fetch time, in a content-addressed, write-once store before it is parsed. An endpoint must pass `igs sources verify` first. The verification fetches a real sample and records its schema, and ingestion stops if a later file's columns or keys differ. `igs rebuild` truncates every derived table and replays the raw store, so the database can always be rebuilt from raw.
- **Normalisation (`igs.normalize`, `igs.xbrl`).**
  - *Prices:* UDiFF and legacy bhavcopy. Only final-session rows are kept; if a key appears in more than one final session, the file is rejected rather than guessed. Delivery data comes from `sec_bhavdata_full` and MTO.
  - *Reference data:* corporate actions (a parser for split, bonus, rights, dividend and demerger subjects), ASM/GSM, holidays, index closes, and the NSE, BSE and Angel One masters.
  - *XBRL:* financial results and shareholding pattern.
  - *Instrument master:* dated ISIN, NSE symbol, BSE code and broker-token ranges, protected by database exclusion constraints. It links symbol renames and post-split ISIN changes, and never links a symbol later reused by a different issuer.
- **Point in time (`igs.pit`).**
  - Every fundamental row carries `period_end`, `filed_at` (the exchange broadcast time) and `ingested_at`, and all factor maths filters on `filed_at`.
  - Restatements are new rows; triggers refuse `UPDATE` and `DELETE`.
  - Each table has one "known at" rule.
  - Factor code can reach data only through `PitView`, and an import-boundary test enforces this.
  - **The look-ahead gate:** every registered factor is computed three ways on a synthetic market full of traps (a late filer, a restatement, a split, a bonus, a missing quarter). The ways are: on all the data, on the data cut off at T, and on the data with every future value corrupted. The three outputs must match. `igs gate run` records a pass for the current code, and the backtest and scoring refuse to run without it.
- **Factors (`igs.factors`).**
  - *Growth:* revenue, EBITDA and profit CAGR over 3 and 5 years, TTM year-on-year growth, acceleration, consistency.
  - *Quality:* ROCE, ROE with DuPont split, operating margin level and trend, cash conversion, net debt/EBITDA, interest coverage, working-capital days.
  - *Valuation:* P/E versus own 5-year median, PEG, EV/EBITDA, P/B, with sector modules; EV/EBITDA is never applied to banks or NBFCs.
  - *Momentum:* 6 and 12-month relative strength against the Nifty 500, price versus 200-DMA, 50/200 state, delivery-% trend.
  - *Ownership:* promoter holding change, pledge level and trend, FII+DII change, institutional holders.
  - Every value carries a status: `ok`, `not_applicable` or `insufficient_data`. Missing data is never imputed.
- **Scoring (`igs.score`).**
  - Winsorise market-wide at the 1st/99th percentile, then z-score within the NSE industry. An industry with fewer than 8 peers falls back to its sector, never to the whole market.
  - Pillar scores and the composite use the YAML weights, renormalised over the factors that apply. Coverage is reported.
  - Red flags are hard filters. A flag whose data is missing is *data unavailable*, never a pass, and that blocks *High conviction*.
  - Each stock gets its top five factor contributions (raw value, peer percentile, source filing) and a "why this stock" text built only from those rows. All generated text passes an advice-language filter.
  - Factors the latest backtest marked DROP are refused.
- **Backtest (`igs.backtest`).**
  - Rebalances monthly (primary) or quarterly, at quarter end + 60 days.
  - Uses the same universe, factor and scoring code as production. The universe is rebuilt at each date from what traded then, so names later delisted are included.
  - Enters at the next close and measures total-return forward returns at 3, 6 and 12 months.
  - Reports decile portfolios, the top-decile NAV (CAGR, volatility, max drawdown, turnover, hit rate) net of STT, stamp duty, exchange, SEBI and DP charges, GST and a square-root market-impact model.
  - Computes Spearman IC per factor, with t-statistics on non-overlapping observations.
  - Walk-forward factor selection uses only IC already realised at each date.
- **Outputs.**
  - A FastAPI app, `igs api`.
  - A Streamlit UI, `igs ui`: rankings with filters and CSV export; stock detail with factor breakdown, eight-quarter trends, shareholding, filings feed and red-flag panel; watchlist; saved screens; run and data-quality details.
  - Alerts from `igs daily` or `igs alerts`: new top-decile names, newly tripped red flags on watchlist names, results filed by watchlist names, pledge changes. They are deduplicated and delivered by email or Telegram.

## Getting started

```bash
uv sync --all-groups                        # Python 3.12; the 'ui' group adds Streamlit
createdb igs                                # PostgreSQL 16
export IGS_DATABASE_URL=postgresql://igs:igs@localhost:5432/igs
uv run igs db migrate

# 1. Prove every endpoint returns real data (needs network access to NSE/BSE)
uv run igs sources verify && uv run igs sources list

# 2. Load history
uv run igs ingest static nse_equity_list nse_trading_holidays bse_scrip_master angel_scrip_master
uv run igs ingest prices --start 2014-01-01 --end 2026-09-22
uv run igs ingest range nse_corporate_actions --start 2014-01-01 --end 2026-12-31
uv run igs ingest range nse_announcements --start 2024-01-01 --end 2026-09-22
uv run igs master rebuild
uv run igs ingest symbols nse_quote_equity          # industry classification
uv run igs ingest static nse_financial_results_index nse_shareholding_index
uv run igs ingest documents financial_results
uv run igs ingest documents shareholding

# 3. Check it before trusting it
uv run igs recon --start 2024-01-01 --end 2026-09-22
uv run igs validate fundamentals

# 4. Gate, backtest, score
uv run igs gate run
uv run igs backtest --start 2016-01-01 --end 2025-06-30
uv run igs score

# 5. Look at it
uv run igs ui          # http://localhost:8501
uv run igs api         # http://localhost:8000/docs
```

For daily use, install `scripts/crontab.example`: it runs `igs daily` after the evening bhavcopy and re-verifies every source weekly. Screener.in exports can be added with `igs import screener <file> [--nse CODE]`. They are stored as tier-3 enrichment and never feed the point-in-time maths. `igs import yfinance` loads fallback prices that are flagged as unverified.

Environment variables:
- `IGS_DATABASE_URL`, `IGS_RAW_ROOT` (default `data/raw`), `IGS_CONFIG_DIR`, `IGS_GATE_PATH`, `IGS_IC_STATUS`.
- Email alerts: `IGS_SMTP_HOST/PORT/USER/PASSWORD`, `IGS_ALERT_FROM`, `IGS_ALERT_TO`.
- Telegram alerts: `IGS_TELEGRAM_TOKEN`, `IGS_TELEGRAM_CHAT_ID`.

## Configuration

| File | What it controls |
|---|---|
| `universe.yaml` | NSE mainboard (EQ, BE), market cap > ₹500 cr, at least 8 quarters filed as of the date. SME and ASM/GSM excluded unless enabled. Buckets use the SEBI method (rank by 6-month average market cap: large = top 100, mid = 101–250, small = 251+). Sector and industry filters. |
| `scoring.yaml` | Pillar weights Growth 35 / Quality 25 / Valuation 15 / Momentum 15 / Ownership 10, equal within each pillar. Enabled factors, valuation modules, winsorisation, peer groups, tier cut-offs, whether IC status is respected. |
| `backtest.yaml` | Monthly rebalance plus a quarterly sensitivity run, horizons 3/6/12 months, deciles, benchmark, IC gate (t ≥ 2 on non-overlapping observations), walk-forward selection. |
| `costs.yaml` | Statutory charges, brokerage, impact model, assumed capital. **Re-check the rates against current circulars.** |
| `red_flags.yaml` | Pledge > 20% of promoter holding; promoter holding down > 5 pp over two quarters; receivable days > 1.5× the 3-year median; at least 2 primary issues adding up to more than 10% of shares in 3 years; ASM/GSM; contingent liabilities > net worth; other income > 25% of PBT; resignations; audit qualification. |
| `sources.yaml` | Every endpoint, its tier, format and session handling; UDiFF final-session IDs; allowed hosts for XBRL documents. |
| `xbrl_concepts.yaml` | SEBI in-capmkt element → concept mapping per taxonomy version; shareholding axes and members. |
| `hand_checked.yaml` | The 20 validation companies (bank, NBFC, two EMS firms, two commodity cyclicals, …). The values are left for a person to type in. |
| `alerts.yaml` | Alert rules and channels. |

## Known limitations

- **Unverified parsers.** Parsers and the XBRL element mapping have not yet seen a real payload. The verification gate and schema fingerprints make a format mismatch fail loudly rather than load wrong numbers.
- **Missing history.** Historical index membership, historical ASM/GSM stages and the four-level industry classification before 2023 are not available from current files. Surveillance filtering in backtests is limited to dates covered by landed snapshots. Industry labels before they were first observed use the earliest known label.
- **Annual-report-only data.** Some red-flag inputs (contingent liabilities, audit opinion) may only be in annual reports. Until they are loaded, those flags report *data unavailable*.
- **Costs and market cap in the backtest.** One current schedule of statutory charges is applied to the whole backtest. Historical market cap is approximated with today's share count on adjusted prices.

## Development

```bash
uv run ruff check src tests
IGS_TEST_DATABASE_URL=postgresql://igs:igs@localhost:5432/igs_test uv run pytest
uv run pytest -m lookahead        # the gate on its own
```

- **Database tests** need a disposable database whose name contains "test", because each test drops and recreates the schema.
- **Test data.** Parser tests use payloads built in the documented formats (`tests/documented_payloads.py`, `tests/documented_xbrl.py`). Factor, backtest, scoring, UI and alert tests use a deterministic synthetic market (`tests/synthetic_market.py`). Real captured samples belong in `tests/fixtures/real/` once the exchanges are reachable.

## Layout

```
config/                 YAML configuration (see above)
scripts/                cron example
src/igs/
  ingest/               raw store, fetcher (GET only), verification, jobs, tier-3 imports
  normalize/            exchange parsers, loaders, instrument master, ISIN rules, adjustment
  xbrl/                 instance parser, concept mapping, listings, shareholding, validation
  pit/                  knowledge-time rules, PitView, loader, look-ahead harness and gate
  factors/              registry and 32 factors (growth, quality, valuation, momentum, ownership)
  score/                normalisation, composite, red flags, tiers, explanations, persistence
  backtest/             calendars, engine, costs, metrics, report
  recon/                reconciliation checks and report
  alerts/               rules and delivery
  api/  ui/             FastAPI app, Streamlit app and charts
  universe.py service.py daily.py cli.py
tests/
```
