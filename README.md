# IndiaGrowthScreener

For public hosting at **stocks.ednis.ai**, follow [the Auth0 + MFA deployment guide](docs/PUBLIC_HOSTING.md). Public access is closed until authentication is configured; the API now requires a separate bearer token.


> **Personal research tool. Not investment advice. Rankings and tiers are screening results from public data and may be wrong or stale; they are not recommendations. AI calls are a language model's judgement, checked only by their own record; the decision and its risk are yours.**
>
> **Regulatory note.** This tool is built for the author's own research. Sharing its rankings, tiers, AI calls, reports or alerts with other people, whether free or paid, in a group chat, on social media or through a newsletter, may amount to providing research or recommendations. That can attract obligations under the SEBI (Research Analysts) Regulations, 2014, including registration. Get proper advice before distributing any output.

IndiaGrowthScreener ingests public NSE/BSE data. It computes a transparent multi-factor score for Indian listed equities (momentum, quality, value, low volatility, growth and ownership), point in time, within industry peer groups. It sorts the universe into tiers: *High conviction*, *Watchlist*, *Not shortlisted* and *Rejected, with reason*. Every number traces back to the filing row it came from.

The screen itself does not place orders, give buy/sell calls or target prices, or use black-box ML, and v1 depends on no paid data. Two optional features sit beside it and never feed the ranking: the AI's buy / hold / sell calls on single stocks (see "AI buy / hold / sell calls" below), and rule-based intraday calls from Upstox candles (see "Intraday calls (Upstox)"). An intraday call becomes a real Upstox order only after an administrator has saved a trading token, switched live trading on (it starts off) and approved that call. A test fails if an order endpoint appears anywhere but that approval-gated module, `src/igs/intraday/trading.py`.

## Status

All seven build steps and a safeguards layer are implemented and tested (786 tests; CI runs lint, the look-ahead gate and the full suite against PostgreSQL 16).

**First contact with live data (2026-09-23).** From the cloud environment, 17 of 22 sources verified against the live endpoints and every parser was run on the real payloads; samples are kept in `tests/fixtures/real/` as regression tests. What it found and fixed:

- NSE truncates corporate-action subjects ("Dividend - Re 1 Per Sh"); the parser now accepts that. All 248 real actions parse.
- Real results XBRL uses the 2020 taxonomy entry point, now accepted, with every mapped P&L element name confirmed present. The audit-opinion element's real name was added.
- **Every column in an NSE XBRL instance declares the same period**; the quarter and the year-to-date column differ only by context id. The parser now takes periods from the ids and the filing's own dates, loads only verified columns, and drops (and reports) any value that conflicts within a filing instead of keeping whichever came first.
- Values are in full rupees; "Lakhs" is only the presentation level. A new check compares each quarter's profit with EPS × shares to catch unit mix-ups.
- NSE's CDN rate-limits bursts (every request refused for about five minutes). The fetcher now spaces NSE requests 5 s apart, waits out that throttle once, and does not re-prime a refused session.

What does not work from the cloud environment: NSE's bot protection refuses dated and historical API queries (results listings for past quarters, older announcements) and the per-symbol quote API; BSE's API refuses as well. Files on the NSE archives host (bhavcopies, delivery, masters, index closes, XBRL documents) are served. Running ingestion from an Indian residential connection is the next step; see `config/sources.yaml` for the per-source notes.

**Integrated Filing (2026-09-24).** Results from the March-2025 quarter onwards are filed through NSE's Integrated Filing system. Its listing endpoint was found from NSE's own page, and a real page of it was captured in a browser. The cloud environment is refused that endpoint, so it still has to pass `igs sources verify` from a residential connection. Three real Integrated Filing XBRL documents (a non-financial audited Q4, an NBFC, an unaudited Q1) were fetched and parsed; every statement identity holds on them. What they changed:

- The listing is paged, newest first, and includes revisions of old quarters. The new `paged` source kind walks it (`igs ingest pages`). A daily run stops at the first page with nothing new; `--backfill` walks it all and can resume from a page. A page that repeats the previous one is an error, because the page numbering is not verified. A filing is dated by the listing's `creation_Date`, the moment its XBRL was created, which is never earlier than the broadcast time.
- **NBFC filings tag interest income with the element banks use for their top line.** Every NBFC would have been classified as a bank whenever no industry classification was loaded, which is the situation today. The form now comes from the filing's entry-point namespace (`IntegratedFinance_NBFC`) and is recorded on the filing. Where no namespace says, a bank is recognised by interest earned *without* revenue from operations.
- Q4 filings state the reporting period twice, for the quarter and for the year. The quarter was chosen only because it happened to come first in the file; the current column is now taken explicitly.
- Past the last page the listing answers with an empty `data` list, which the schema check had treated as a format change. Verification still refuses empty samples.
- Asking NSE's CDN a second time for a document it has already served was refused (403). Documents are fetched once and kept.

**Evidence review and re-weighting (2026-09-24).** The app was compared with published evidence on what predicts Indian equity returns, and with NSE's factor indices, Indian screeners and quant funds. Changes that followed:

- **Weights follow the Indian evidence, not a growth tilt.** Momentum, quality, value and low volatility get 20% each, as NSE's multi-factor index weights them equally. Growth gets 15% and ownership 5%. In long-only BSE 200 tests for 2007-2021 (Raju & Teli 2022), volatility-adjusted momentum, low volatility and quality earned the highest alphas after costs, while a multi-parameter growth screen trailed the index. These weights are still a prior: no backtest on real data has tested them, and every run says so.
- **A low-volatility pillar** (one-year volatility of daily returns, lower is better).
- **Momentum is return divided by volatility over 6 and 12 months**, as in NSE's momentum indices. Raw relative strength, the moving-average signals, delivery % and changes in institutional holdings stay on the stock page and in the backtest at weight 0, because no Indian study shows they predict returns.
- **The backtest keeps a factor only at t ≥ 3**, up from 2, because hundreds of published factors make t ≥ 2 too easy to pass by chance (Harvey, Liu & Zhu 2016).
- **High conviction could never be reached.** The contingent-liabilities check could not be evaluated for any company, because the figure is in annual-report notes and not in quarterly results, and a reject check that cannot be evaluated blocked the tier. Such a check can now be configured not to block (`unavailable_blocks: false`); it is still shown as not checked, and it still rejects once a source provides the figure. Separately, the tier needs 80% factor coverage, which was impossible without the 35% growth pillar; growth, which needs the longest history, now carries 15%.
- **A crash on short price histories.** The 6- and 12-month return lookups raised an error for the whole factor whenever one company had fewer sessions than the lookback, such as a recent listing. That company now gets *insufficient data*.
- **No prior-year column in the XBRL.** The real NSE results instances checked contain only the current quarter, year to date and balance-sheet columns, so earlier quarters cannot be recovered from later filings.

**Insider trades (2026-09-25).** Disclosures under SEBI's insider-trading (PIT) regulations are the one missing data stream with solid Indian stock-level evidence: disclosed insider purchases were followed by abnormal returns of about 6.7% over 90 days in a 2026 study. They are now ingested from NSE (`nse_insider_trading`, also in the daily job).
- **Point in time.** Each trade is known only from the exchange broadcast, never from the trade date or the date the company was told. A row whose broadcast seems to precede the intimation is skipped and reported.
- **Scoring.** A new ownership factor, `insider_buying_90d`, counts open-market purchases of equity by promoters, directors and key managers in the last 90 days, as a percentage of market cap. Sales, ESOPs, off-market transfers and trades by other employees do not count. A company with no purchase scores 0, but only when the loaded disclosures cover the whole 90 days; otherwise the factor is *insufficient data* for everyone.
- **Where it shows.** Each stock page has a table of the last 12 months of disclosures, and a watchlist alert fires on open-market trades by insiders.
- **Not yet seen a real row.** From the cloud the endpoint answers with an empty list. The field names come from an existing open-source client. A row without them stops the load and names the fields it has, and unrecognised transaction types, modes and person categories are kept verbatim and reported, never guessed. (Real rows later matched these names; see the next entry.)

**Insider trades from NSE's current system (2026-09-29).** NSE moved insider-trading disclosures to a new system around May 2026. The endpoint above (`nse_insider_trading`) still serves full rows for earlier dates, but an empty list for recent ones, which is why it looked empty. The current listing was found from NSE's Insider Trading page in a browser. Real samples of both, and two of the new XBRL files, are in `tests/fixtures/real/`.
- **Two steps, like results.** The listing (`nse_insider_disclosures`) gives one row per disclosure, with its broadcast time and a link to its XBRL. `igs ingest documents insider_trading` then fetches each XBRL once and loads its trades. The NSE check does both every 30 minutes.
- **A different XBRL.** The new files use BSE's `in-bse-co` taxonomy, with one context per trade. Holdings are fractions there (0.0021 means 0.21%), so they are stored as percent; a value above 1 is reported and not stored.
- **Revisions.** NSE lists a corrected disclosure as a revision without saying which disclosure it corrects. A revision replaces the earlier rows for the same person and trade date, from the moment it is broadcast. The stock page marks the replaced rows. This is factor code, so run `uv run igs gate run` again after updating.
- **History.** `nse_insider_trading` is now verified on 1-7 April 2026 and used for dates before May 2026. A trade listed by both systems around the changeover is loaded once.
- **New spellings.** "Pledge Revoke", "Pledge Invoke", "Revokation of Pledge" and "ESOS" are now known values. Disclosures NSE lists with no person or quantity are skipped and reported once per payload.

**Industry when the quote API is refused.** NSE's four-level industry classification comes from the per-symbol quote API, which is refused to the cloud environment. Without an industry, no factor has peers, so nothing can be scored. Every NSE announcement carries the company's industry under NSE's older single-level labels (`smIndustry`, e.g. "Pharmaceuticals", "Finance - Housing"). A company without the four-level classification now takes the latest label on its announcements known at the scoring date. Labels group peers at the industry level only; there is no sector above them, so a label with fewer than 8 peers leaves its companies unscored ("insufficient peers") rather than comparing them with unrelated companies. The catch-all "Miscellaneous" is not used. "Banks" selects the bank module; "Finance", "Finance - Housing" and "Financial Institution" select the NBFC module. Results store and show which source a company's industry came from. Coverage grows with announcement history: one real week labelled 563 of the 1,491 companies that announced something, never with two different labels. Announcements loaded before this change get their labels with `igs rebuild`. Where the quote API answers, each check (`igs sync`, every 30 minutes) also asks it for up to 25 companies without the four-level classification, those in the latest ranking first; a symbol it gives none for is asked again after a week. Where it is refused, the check only says so. **Sector from NSE's index list.** NSE's Nifty Total Market list (the Nifty 500 and Microcap 250, about 750 companies) gives each member's sector, the sector level of the four-level classification, from nsearchives, which also serves the bhavcopies. Each check loads it at most once a day. A company without NSE's sector takes the list's, point in time from the day it was loaded, so a company with no industry, or a label with fewer than 8 peers, is compared with its sector instead of left unscored.

| # | Step | Built | Still to do with real data |
|---|---|---|---|
| 1 | Ingestion, instrument master, adjusted prices, reconciliation report | yes | `igs sources verify`, backfill, run `igs recon` and review the report |
| 2 | XBRL parser (2022 and 2024 taxonomies, both NSE filing systems), shareholding, 20-company validation | yes | Confirm element names against real instances (the mapping lives in YAML); type hand-checked values into `config/hand_checked.yaml`; run `igs validate fundamentals` |
| 3 | Factor library (36 factors) and unit tests | yes | - |
| 4 | Walk-forward backtest, costs, factor IC report | yes | Run on 10+ years of data; drop factors the IC gate rejects |
| 5 | Scoring and API | yes | Tune the tier thresholds once real IC results exist |
| 6 | UI (Streamlit) | yes | - |
| 7 | Alerts (email, Telegram) and daily job | yes | Set the channel credentials; install the cron entry |
| 8 | Safeguards: 25 red flags and cautions, robustness gates, data sanity, run health, failure measurement | yes | Run the backtest; read the failure-rate and check-effectiveness tables; adjust severities and thresholds from them (see below) |

Sources whose URL is still unknown are `url: null` in `config/sources.yaml`: delisted securities and the Nifty 500 TRI. Until the TRI is found, the backtest benchmarks against the Nifty 500 price index and says so loudly in every report.

## Keeping failures rare, and measuring how rare

No screen can make a loss impossible: prices move on news nobody has yet. What the screen *can* do is remove the known, avoidable ways a top-ranked stock turns out badly, and then measure, out of sample, how often the names it vouches for still fail. A stock is *High conviction* only if **all** of the following hold; any one failing moves it to Watchlist and the reason is shown:

1. **Its data can be trusted.**
   - No power-of-ten jumps in revenue, total assets or restated figures. A filing tagged in lakhs while declaring rupees is the classic way a screen puts garbage at the top.
   - Accounting identities hold inside each recent filing.
   - The next results are not overdue past the SEBI LODR deadline.
   - No factor value is outside its plausibility bounds. Such a value is left out and logged; it is never clipped.
   - Profit growth measured from a base under 2% of revenue does not count as growth.
2. **Governance and earnings quality are clean.** Reject-severity red flags (e.g. pledge, promoter selling, auditor/CFO resignation, audit qualification, receivable spike, repeated dilution, surveillance, contingent liabilities, other income share, profits not converting to cash over 3 years) and the cautions (accruals, Altman Z'' distress zone, weak Piotroski F-score, Beneish M-score, large cash and debt with little interest earned, exceptional items, restated figures).
3. **It is not a market-behaviour risk.** Cautions for thin trading, volatility above 60%, a 300%+ run-up in a year, a 50%+ fall from the 1-year high, and the trade-for-trade segment.
4. **Every check could be evaluated.** Missing data is *data unavailable*, never a pass.
5. **The rank is robust.**
   - It stays in the band in at least 60% of 200 seeded redraws of the pillar weights.
   - It was in the top 30% at each of the previous two month-ends, with those ranks computed point in time.
   - At least 3 pillars score above zero and none is below -1.
   - No single factor carries more than 40% of the score.
6. **The run itself is healthy.**
   - Prices, announcements, ASM/GSM lists and results filings are fresh.
   - The universe, coverage, factor availability and top decile have not shifted implausibly since the last run.
   - An unhealthy run is stored, alerted on, and publishes no High conviction names at all.

Then the backtest measures what that bought, using exactly the production code at every rebalance:

- **Failure definition** (`backtest.yaml`): within 12 months of entry, a total return of -30% or worse, a 40% fall from the running high, or the stock stops trading.
- **Failure rate of every tier**, with a 95% Wilson interval, against the whole universe and against "top decile by score alone, no checks". The safeguards have to beat that last row, by more than the uncertainty, for the report to say they helped.
- **Check effectiveness.** Among top-ranked names, did the names that tripped each check fail more often than those that did not? Verdicts: SUPPORTED, NO EVIDENCE, CONTRADICTED, INSUFFICIENT DATA. A check the data contradicts is removing names that did better, and should be reconsidered.
- **Threshold sensitivity.** Alternative robustness thresholds are compared on the first half of the dates and confirmed on the second, so thresholds are not tuned on the data that judges them.

Expect High conviction to be a short list, and sometimes empty. That is the intended trade: fewer names, each checked more ways. Every threshold is in YAML and is a starting point until the real backtest has been read.

## How it works

```
raw landing zone (immutable) -> normalize -> point-in-time view -> factors -> score -> API / UI / alerts
        ^ igs sources verify                  ^ look-ahead gate             ^ backtest IC gate
```

- **Ingestion (`igs.ingest`).** Transient HTTP failures (timeouts, 429, 5xx) are retried with backoff, and an expired NSE session is re-primed once; every attempt is landed. Every response is stored verbatim, with its fetch time, in a content-addressed, write-once store before it is parsed. An endpoint must pass `igs sources verify` first. The verification fetches a real sample and records its schema, and ingestion stops if a later file's columns or keys differ. `igs rebuild` truncates every derived table and replays the raw store, so the database can always be rebuilt from raw.
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
  - **The look-ahead gate:** every registered factor, every red flag and caution, and the whole scoring pipeline (including the rank history used for persistence) are computed three ways on a synthetic market full of traps (a late filer, a restatement, a split, a bonus, a missing quarter). The ways are: on all the data, on the data cut off at T, and on the data with every future value corrupted. The three outputs must match. `igs gate run` records a pass for the current code, and the backtest and scoring refuse to run without it.
- **Factors (`igs.factors`).**
  - *Growth:* revenue, EBITDA and profit CAGR over 3 and 5 years, TTM year-on-year growth, acceleration, consistency.
  - *Quality:* ROCE, ROE with DuPont split, operating margin level and trend, cash conversion, net debt/EBITDA, interest coverage, working-capital days.
  - *Valuation:* P/E versus own 5-year median, PEG, EV/EBITDA, P/B, with sector modules; EV/EBITDA is never applied to banks or NBFCs.
  - *Momentum:* 6- and 12-month return divided by one-year volatility (weighted); the same returns ending a month ago, the academic form that skips the latest month's short-term reversal, 6 and 12-month relative strength against the Nifty 500, price versus 200-DMA, 50/200 state and delivery-% trend (tracked at weight 0).
  - *Low volatility:* annualised volatility of daily returns over one year.
  - *Ownership:* pledge level and trend, promoter holding change, insider buying (open-market purchases of equity by promoters, directors and key managers disclosed under SEBI's insider-trading rules in the last 90 days, % of market cap) (weighted); FII+DII change and institutional holders (tracked at weight 0).
  - Every value carries a status: `ok`, `not_applicable`, `insufficient_data` or `unfavourable`. Missing data is never imputed. *Unfavourable* means there is no value because the company's own figure is negative where the measure needs a positive one: a loss (P/E, PEG), a profit decline (PEG), negative EBITDA (EV/EBITDA, and net debt/EBITDA with net debt), negative equity (P/B), or a profit or EBITDA that turned into a loss (their growth). Scoring ranks it with the worst of its peers instead of leaving it out, so a loss cannot make valuation look neutral. A turnaround from a loss stays *insufficient data*.
- **Scoring (`igs.score`).**
  - Winsorise market-wide at the 1st/99th percentile, then z-score within the NSE industry. An industry with fewer than 8 peers falls back to its sector, never to the whole market. Without the four-level classification, the industry is NSE's label on the company's announcements (no sector level); the source is stored with each result.
  - Pillar scores and the composite use the YAML weights, renormalised over the factors that apply. Coverage is reported.
  - Checks have a severity (`red_flags.yaml`): a tripped *reject* check is a hard filter (Rejected, with the reason); a tripped *caution* keeps the stock out of High conviction. A check whose data is missing is *data unavailable*, never a pass, and blocks *High conviction* unless the check is configured `unavailable_blocks: false` (used for inputs the loaded filings never contain, such as contingent liabilities).
  - Robustness gates, plausibility bounds and run health (above) decide High conviction among the top-ranked names; every blocking reason is stored with the run and shown.
  - Each stock gets its top five factor contributions (raw value, peer percentile, source filing) and a "why this stock" text built only from those rows. All generated text passes an advice-language filter.
  - Factors the latest backtest marked DROP are refused.
- **Backtest (`igs.backtest`).**
  - Rebalances monthly (primary) or quarterly, at quarter end + 60 days.
  - Uses the same universe, factor and scoring code as production. The universe is rebuilt at each date from what traded then, so names later delisted are included.
  - Enters at the next close and measures total-return forward returns at 3, 6 and 12 months.
  - Reports decile portfolios, the top-decile NAV (CAGR, volatility, max drawdown, turnover, hit rate) net of STT, stamp duty, exchange, SEBI and DP charges, GST and a square-root market-impact model.
  - Computes Spearman IC per factor, with t-statistics on non-overlapping observations.
  - Walk-forward factor selection uses only IC already realised at each date.
  - Runs the full production tiering at every date and reports failure rates by tier, check effectiveness and threshold sensitivity (above).
- **Outputs.**
  - A FastAPI app, `igs api`. `GET /companies?q=...` finds a company by any words of its name or its NSE symbol.
  - A Streamlit UI, `igs ui`: rankings with filters and CSV export; stock detail (also for companies outside the ranking, such as a new listing, from its first price file on) with key numbers (price, market cap, 52-week high and low, P/E and the industry's median P/E, book value, P/B, EPS, dividend yield, ROCE, ROE, sales and profit growth, margins, debt/equity, promoter holding and pledge, returns), factor breakdown, eight-quarter trends, shareholding, filings feed and red-flag panel; watchlist; saved screens; run and data-quality details. Wherever you pick a stock (the stock page, adding to the watchlist, opening an AI call), type any part of the company's name or its symbol. The rankings search matches every word typed, in any order, and ignores "Ltd" and "Limited".
  - New files from NSE, `igs sync`: the UI checks when it starts and every 30 minutes while it is open, and a scheduled job can do the same when it is closed. A check runs every ingest step but downloads only what is not loaded yet. Only one check runs at a time, a check that isn't forced waits 25 minutes after the last one, and every check is recorded and shown in the sidebar. It does not re-score; the daily job does.
  - Alerts from `igs daily` or `igs alerts`: runs that withheld High conviction (and why), names entering or leaving High conviction, new top-decile names, newly tripped red flags and cautions on watchlist names, results filed by watchlist names, pledge changes, open-market insider trades on watchlist names. They are deduplicated and delivered by email or Telegram.

## Automatic news in India's context

The **News** page collects economy, defence and international RSS summaries from The
Economic Times. Collection runs with the existing app startup/periodic sync and daily
workflows. Run `uv run igs news collect` for an immediate check—no JSON file is needed.
AI assessments explain potential impact on Indian industries, with bounded rating
adjustments and source evidence. Articles no rating used (no company matched, or never
assessed) are deleted 30 days after publication. See [setup and limitations](docs/GEOPOLITICAL_NEWS.md)
and [automatic ingestion schedules](docs/SCHEDULES.md).

## Market sentiment in the scores (experimental)

Two capped layers, both switchable in `config/scoring.yaml` (`sentiment`). Both read only
what was known at the as-of date, go through the look-ahead gate, and run inside the
backtest like any other rule.

**The market's mood tilts the pillar weights.**
- **What it is:** a mood from -1 (fear) to +1 (greed). It is the average of five
  readings from prices the app already loads:
  - the share of the universe above its 200-day average;
  - the Nifty 500 against its 200-day average;
  - rises against falls over the last 20 sessions;
  - stocks at 52-week closing highs against lows;
  - the Nifty 500's volatility against its past year.
- **Why weights and not scores:** every stock is in the same market, so a mood can change
  the ranking only through what the ranking rewards.
- **The tilt:** when the mood is negative, momentum loses weight and quality and low
  volatility gain it; when positive, the reverse.
  - It is continuous, at most 25% of a pillar's own weight (momentum's 20% moves between
    15% and 25%).
  - Why these pillars: momentum profits have followed rising markets and collapsed in
    rebounds after falls, and quality held up in crises. The sources are in
    `scoring.yaml`; the tilt is not yet tested on Indian data.
- **Where you see it:** a banner on the rankings page gives the mood, each reading and
  the weights this run used. Each run stores it (`score_run.market_sentiment`).

**Each stock's own sentiment is a capped overlay**, like the geopolitical one: at most
±0.10 on the composite (z-score units).
- **Brokers' calls:** each broker's latest research call in the last 30 days counts
  once: +1 buy, 0 hold, -1 sell. An upgrade or downgrade from that broker's previous
  call counts +1 or -1, since rating changes have carried more information than rating
  levels.
  - Targets are shown on the stock page but not scored: large implied upsides have
    mostly not been reached.
  - Short-term trading ideas are left out.
- **News tone:** the AI reads each stock-news article (the Economic Times and Moneycontrol
  feeds in `config/broker_calls.yaml`) and gives, for each company the article is about,
  the tone for its shareholders from -1 to +1, with a confidence and the article's own
  words.
  - A quote that is not in the article is not stored.
  - Brokers' ratings and bare price moves are not news.
  - It runs on every check once the assistant is on (`features.news_tone`, about ten
    small requests a day).
- **How items combine:** each item fades with its age (brokers over two weeks, news over
  five days). The average is shrunk by n/(n+1), so a single call or article counts half
  as much as a strong consensus.
- **No calls and no news mean no adjustment.** Calls and tones count only from when the
  app recorded them, so a call pasted today for last month does not change past runs,
  and the backtest can measure this only going forward.
- **High conviction:** a stock that reaches the band only through a positive adjustment
  is held at Watchlist, with the reason shown. It is not validated yet.
- **Where you see it:** the stock page's "Sentiment adjustment" lists the calls and
  articles behind it, and the stock's explanation says what it rests on. The AI's own
  calls receive the mood and the stock's sentiment with the rest of their data.

After updating: `uv run igs db migrate`, then `uv run igs gate run` (the scoring code
changed), then `uv run igs score`.

## Research assistant (optional, AI)

An optional assistant uses the Claude API to make a run easier to work through. It is off until you enable it and save an API key on the UI's **Settings** page (or in `.env` and `config/assistant.yaml`):

- **Ask** (`igs ask "..."`, the UI's Ask page, `POST /ask`). Questions about a run are answered through read-only lookups into its stored results: run overview, rankings with filters, one stock's result and factor table, eight quarters, shareholding, filings and announcements. For example: "Why is X not High conviction?", "Which watchlist names tripped a caution, and why?", "Compare the quality pillar of A and B". Every lookup is pinned to the run being discussed and listed with the answer.
- **Briefs** (`igs assistant brief SYMBOL`, a button on the stock page, `GET /stocks/{symbol}/brief`). A plain-language summary of one stock's result: where it stands, what lifts and what holds back its score, checks and data gaps, and which filings to read. It is stored per run, so it is paid for once.
- **Announcement notes** (`igs assistant read-announcements`, and the daily job when enabled). New announcements by companies in the universe or on the watchlist get a category, a materiality level, a one-sentence factual summary and any governance concern the announcement states (an auditor or key-person resignation, a default, a pledge, fraud, ...). High-materiality notes and notes naming a concern raise alerts for watchlist names.

What it never does:
- **Scoring.** Ask, briefs, announcement notes and AI calls do not affect ratings. Two stored AI readings do, each as a bounded, experimental adjustment: the explicit [geopolitical news feature](docs/GEOPOLITICAL_NEWS.md) stores evidence-linked assessments, and the news tone feeds the stock sentiment overlay ("Market sentiment in the scores"). Scoring and backtests never call an AI model: they use only assessments recorded by the as-of date, preserving historical results.
- **Unlabelled output.** Everything it writes is labelled as AI output.
- **Recommendations outside AI calls.** Ask, briefs and notes pass the same buy/sell/target-price guardrail as the rest of the app. A slip gets one rewrite, and is withheld if the rewrite slips too. Only the AI calls below make calls.

### AI buy / hold / sell calls

The AI reads everything the app holds on a stock at the run's date. It then decides **buy** (open or add now), **hold** (keep it if you own it, don't add) or **sell** (exit if you own it).

**Automatic calls.** After scoring, the daily job decides which stocks need a new call, from what was ingested.
- **Covered stocks:**
  - your watchlist;
  - the 100 best-ranked stocks that aren't rejected (`top_ranked`);
  - every stock with a broker's call in the last 7 days (below);
  - every stock whose latest call is buy or hold, because you may own it, and a sell must reach you.
- **When a covered stock gets a new call:** it gets one when something arrived since the data its last call saw:
  - new results or a shareholding filing;
  - an insider trade by a promoter, director or key manager;
  - a material announcement;
  - a broker's call;
  - a tier change;
  - a newly tripped red flag or caution.

  It also gets one if it has no call yet (a stock that enters the top 100 gets its first call in that evening's daily job), and every week: when its last call is more than 7 days old.
- **Order and limit:** the most urgent go first (a tier change or red flag, then new data, then first calls, then the weekly refreshes), up to 120 a day and within the daily spending threshold.
- **Each call records why it was made.** The AI also sees its own previous call on the stock and checks whether that call's "when to buy" and "when to sell" conditions are now met.
- **Where you see them:**
  - the rankings have an AI call column;
  - the **AI calls** page lists what is due next and has a button to make those calls now;
  - `igs assistant auto-calls` does the same from the command line.
- **Settings** has the numbers (stocks covered, days, calls a day).

You can also ask for a call on any stock page or with `igs assistant call SYMBOL`.

**What it reads:**
- rank, tier and pillar scores;
- every factor with its peer percentile;
- red flags and cautions, and the robustness tests;
- eight quarters of results;
- shareholding and pledge;
- insider trades of the last 12 months;
- filings and announcements;
- the news adjustment;
- a price summary against the Nifty 500: returns, 52-week range, 50- and 200-day averages, volatility and turnover;
- the key numbers (P/E and the industry's median P/E, EPS, book value, debt/equity, dividend yield, promoter holding);
- brokers' calls on the stock in the last 90 days, with their targets.

**What each call gives:**
- a confidence and a horizon;
- the reasons, citing figures;
- the risks;
- **when to buy** and **when to sell**, as conditions you can check later in results, filings, prices or the screen;
- the data gaps that limited it;
- how it compares with the brokers' calls, and why it agrees or disagrees;
- **its verdict on each broker's call** it was shown: agree, partly agree, disagree or cannot judge, with the reason in a sentence.

**Brokers' calls.** Brokers' buy, hold and sell calls are a second opinion the AI weighs. In the ranking they count only through the capped stock sentiment adjustment ("Market sentiment in the scores").
- **From the news, on every NSE check** (`config/broker_calls.yaml`):
  - **Moneycontrol.** Its RSS feeds stopped on 23 April 2024, so the app reads its news sitemap, the list of its last 1,000 articles (about two days) that it publishes for search engines and names in its robots.txt. It keeps the stock and market news. A headline such as *Buy Shriram Finance; target of Rs 1220: Motilal Oswal* is recorded as it stands, dated the day it was published.
  - **The Economic Times'** stock-news RSS feeds.
  - **The AI** reads the other articles that mention a rating, a target or a brokerage (for Moneycontrol, the headline and its keywords) and records each explicit call: the broker, the rating as written, buy, hold or sell, the target price and the date. It skips block deals, stake sales and market commentary.
- **Older Moneycontrol calls, pasted.** Open [moneycontrol.com/news/business/stocks](https://www.moneycontrol.com/news/business/stocks/) (and its next pages) in your own browser, copy all of it and paste it on the **AI calls** page ("Import older brokers' calls from Moneycontrol"). Every call headline becomes a call, dated by the report date under it. `igs brokers import FILE` reads a copy saved from the browser.
- **One call per broker.** Every broker's call is its own line, also when several brokers call the same stock on the same day with the same rating and target. A headline naming several brokers ("...: Motilal Oswal, ICICI Securities") gives one call for each, and the AI lists each firm's call in an article separately.
- **The same call twice.** The same broker's call on the same stock, rating and target within 3 days is recorded once, whichever source had it first (the published day on one page, the report's date on another).
- **From you.** Add a single call you read anywhere on the stock page (or with `igs brokers add SYMBOL --broker ... --call buy --target ...`).
- **Matching.** A call is linked to a company by its NSE symbol, or by a name that matches exactly one company. Otherwise it is kept as "not matched", never guessed. Link one yourself on the **AI calls** page ("Calls not matched to a company") or with `igs brokers match ID SYMBOL`; it then counts from that moment, so past runs stay as they were.
- **The AI's verdict, on every call.** Each broker's call on a matched stock gets the AI's verdict: agree (the data supports the rating and the target is reachable over its horizon), partly agree (right direction, but the target or timing is not supported), disagree, or cannot judge, with the reason in a sentence citing the data.
  - **From an AI call.** Every AI call on a stock gives a verdict on each of its brokers' calls of the last 90 days. A new broker's call on a stock the AI covers makes it due for a new AI call.
  - **From a review.** Every other call gets its verdict from a review: the AI reads the stock's data at the latest run and judges each of its brokers' calls (`igs.assistant.verdicts`).
  - **As soon as it arrives.** New manual entries, pasted imports, linked calls and committed collection batches trigger review immediately. A one-minute retry worker picks up calls deferred by another active review; the existing daily review limit and AI budget still apply. Install it with `scripts/install-schedules.sh`, or run `igs brokers review-pending` manually. This reviews calls already in the list; it does not increase feed polling frequency.
  - **Performance since recommendation.** The single calls table compares the recommendation day's close (or first available close within 7 days) with the latest stored NSE close. It shows both price dates, raw and split/bonus-adjusted changes, and direction-aware results: rising prices favour Buy, falling prices favour Sell, and Hold is unscored. Broker rows measure the original broker recommendation, not AI agreement issued later. AI rows start on the actual AI issuance date. Missing, same-day and stale prices do not count in the directional success summary. Results refresh with daily price ingestion; they exclude dividends and costs and are not trade profits or a completed-horizon accuracy measure. The separate Record section retains the existing AI-versus-Nifty-500 horizon analysis.
  - **Assessment confidence.** Each broker verdict has its own AI confidence percentage: Low below 50%, Medium from 50% to below 75%, and High from 75%. The calls table, stock view and new Telegram agreement alerts display it. This is confidence in the assessment, not a calibrated probability of profit. Older verdicts show “Not assessed” and are automatically queued for reassessment within the existing budget. Filling confidence for an unchanged historical agreement does not send a new alert.
  - **Every week.** The daily job, after its AI calls, reviews again each stock whose verdicts are more than 7 days old, while its calls are within the last 30 days (`refresh_days`, `days`). At most 60 stocks a day in all (`features.verdicts`).
  - **At once.** **Ask the AI for its verdict** on the stock page, the button on the **AI calls** page, and `igs assistant verdicts [SYMBOL]`. The AI calls page lists each call waiting for a verdict on its own line, with why.
  - **Stocks outside the ranking** are reviewed too, on what the app holds without the screen: results, shareholding, filings, insider trades and prices, plus a Screener.in export where you imported one (below).
  - **Trading ideas** are judged on the prices: the trend against the 50- and 200-day averages, the 52-week range, returns against the Nifty 500, and whether the target is within the stock's usual moves. "Cannot judge" is kept for calls the data can't test, and the reason says what is missing; such a call is reviewed again once a Screener.in export for the stock is imported.
  - Unmatched calls get a verdict once you link them.
- **Where you see them:**
  - the stock page lists the calls, with each target's upside from the latest close and the AI's latest verdict and reason;
  - the **AI calls** page has one combined calls table with `Source = Broker` or `AI`. Broker rows show their original rating, AI verdict/reason, and confirmed call; independent AI calls retain their own action. Each row's performance runs from the first close it could have been traded at (the next session's, for an AI call made after 15:30) to the latest close. A buy counts as right when it beat the Nifty 500 over those dates and a sell when it lagged it, so a rising market doesn't make every buy look right. Processing queues and imports are grouped below;
  - under each AI call, its verdicts on the brokers' calls it was shown;
  - `igs brokers list` prints them, verdicts included.
- **Telegram confirmation.** An explicit `agree` on a broker Buy/Sell queues one Telegram message immediately when the verdict is saved. Holds, partial agreement, disagreement and unknown verdicts do not qualify. Repeated reviews do not send the same broker call again. Deletion or withdrawal of agreement cancels an unsent message. Failed delivery stays in the durable outbox with the existing retry/backoff limit. Telegram must be configured and its call channel enabled. Existing historical agreements are shown in the table without a notification backfill. Like other Telegram delivery, a crash after Telegram accepts a message but before the local acknowledgement can cause a duplicate.
- **Separate Telegram ranking alerts.** `TOP 10 ENTRY` announces a stock newly ranked 1–10 relative to the preceding score run (all qualifying stocks on the first run). `BROKER + AI AGREEMENT` identifies the confirmed broker Buy/Sell message. `TOP 100 — AI BUY` identifies a current top-100 stock whose latest independent AI assessment is Buy and no more than 30 days old; repeated Buy assessments remain one alert until the AI switches away and back. Entering the top 100 with an existing recent Buy also qualifies. The daily alerts step and the one-minute broker review worker check these events; the worker uses score runs no more than seven days old. Each event gets its own message through `telegram_calls`, with durable retry and deduplication. Unsent ranking messages are cancelled when the stock no longer qualifies. Ranking alerts are controlled by `top10_entries` and `top100_buys` in `config/alerts.yaml`.
- **Cost.** Reading takes one small request for about 15 articles, a few cents a day. A review is one request per stock at `medium` effort, roughly US$0.05-0.20.

**Screener.in exports, for data the app lacks.** Where the app's data on a stock is thin (outside the ranking, few quarters loaded, or a "cannot judge"), a Screener.in export fills in what the AI reads for its calls and verdicts: ten years of results, the balance sheet and cash flows (`igs.screener`).
- **Background or manual exports.** The authorized background process uses Screener's Excel export form, with login, identity checks and account-limit handling. Every 30 minutes it downloads up to 10 exports: first stocks with fewer quarters than the scoring minimum (8), so their history fills and they can be ranked; then stocks with a buy/sell call, then higher scores. Each export refreshes after 30 days. See [setup and recovery](docs/SCREENER_BACKFILL.md). Manual uploads remain available on stock pages, AI calls and `igs import screener FILE...`.
- **A check on the app's figures.** Each export's quarterly and annual sales, profit before tax and net profit are compared with the results filings the app loaded, consolidated and standalone: they agree within 2% (or Rs 0.1 crore), or differ, and figures only in the export are the gaps it fills. The stock page shows the comparison, `igs screener check SYMBOL` prints it, and a difference is logged as a data-quality warning.
- **What the AI is told.** It gets the export's figures up to the run's date, with the check: use them for what the app lacks, trust the filings where they differ, and say when it relies on them.
- **Scoring fallback.** Background exports with a verified reporting basis fill missing quarterly inputs and the minimum-quarter coverage check. Exchange values take precedence on the same basis. Availability starts at import and basis verification, never at the quarter end. Historical scores cannot see later imports; factor details retain source references.
- **The file.** The workbook's company is the one its name matches (or the stock whose page you upload it on); the same file twice is imported once. It is read from its Data Sheet tab by its shape (sections, Report Date rows, labelled lines), with the standard library. It has regression tests using documented-layout workbooks and has been verified with live authenticated exports.

**Its record.** An AI's calls can't be back-tested: for past dates, what happened next is in its training data. So every call is stored with the exact data it was given and never changed. The **AI calls** page measures each call from the last close the model saw against the Nifty 500 after 1, 3, 6 and 12 months:
- a buy is right if the stock beat the index;
- a sell is right if it lagged;
- the page shows the share right and the mean excess return for each action.

Until that record has months of calls behind it, treat the calls as unproven.

**Alerts.** A stock's first call, and any change of action, is sent in the daily alert digest, for every stock the AI covers (`scope: watchlist` in `config/alerts.yaml` limits it to the watchlist). Each alert says what prompted the call.

**Telegram and WhatsApp.** Each new buy or sell call (a stock's first buy or sell, or a change to buy or sell) can also come as its own message. Holds are never sent.
- **Telegram** (free, through Telegram's bot service) gets only these calls, each in brief: the call, confidence, horizon and the last close the AI saw, its summary and its three main reasons. The daily digest of other alerts goes by email, not Telegram.
- **WhatsApp** gets the detailed message: also when to buy and when to sell, risks, what prompted it, and the AI's record so far.
 For WhatsApp there are two services: its official Cloud API (Meta; a template Meta approves, about ₹0.15 a message) and CallMeBot (free, personal use, through its servers). Set them up on the Settings page; docs/DEPLOY.md has the steps.

**Cost.** Each call is one request of about 15,000 input tokens at `high` effort: roughly US$0.10-0.30 with `claude-opus-5`, more at `xhigh` or `max`; a review of a stock's brokers' calls costs about US$0.05-0.20. Covering the top 100 takes about 100 calls (US$10-30) the first time, then about 15-30 calls a day for the weekly refreshes, new entrants and new data (US$2-8), plus 20-40 reviews a day (US$1-6). The daily spending threshold, which all AI features share, decides how much of that runs: at US$2 (the default) only about 10 requests fit and the rest wait, oldest work first, for the next days. About US$10-15 a day lets it all through; `claude-sonnet-5` (Settings) costs less than half as much per request. Calls a day and reviews a day are capped at 120 and 60.

Costs and controls:
- **Model and settings.** It uses `claude-opus-5` by default, with adaptive thinking at a per-feature effort level and prompt caching. Server-side refusal fallbacks are enabled (`fallbacks: default`), so a request declined by the model's safety classifiers is retried on the recommended fallback model instead of failing.
- **Spending.** Every call is logged with its tokens and estimated cost (`igs assistant status`), and a daily budget stops calls once it is reached.
- **Settings page.** It sets the API key, model, daily budget, fallbacks and per-feature effort, tests the connection without using tokens, and shows the week's usage. The key goes into `.env` (readable by you only, never shown in full). Changed settings go into `data/settings/assistant.yaml` on top of `config/assistant.yaml`, so `git pull` never conflicts with them. The page can only change anything while the UI is reachable from this computer alone (the `igs ui` default).
- **What leaves your computer.** Only your question, the looked-up stored results and announcement text are sent to the API. An AI call or a review of brokers' calls also sends that stock's stored results, filings list, insider trades, price summary, key numbers, brokers' calls and Screener.in export figures. Reading brokers' calls sends the news articles' titles and summaries.

## Intraday calls (Upstox)

A separate **Intraday calls** page, outside the ranking and its rules. [docs/INTRADAY.md](docs/INTRADAY.md) has the setup, the exact rules and their limits.
- **The scan.** Every five minutes from 09:30 to 15:15 IST, up to 100 stocks are checked on closed five-minute Upstox candles. Stocks with a recent AI or broker buy/sell call or an investor disclosure come first, then higher scores. Only stocks Upstox currently allows for intraday (MIS) trading are scanned.
- **A buy** needs a 15-minute move of at least +0.3%, a close above VWAP and the first 15 minutes' high, volume at least 1.8× the same five minutes' median over up to 20 sessions, ₹10 crore traded so far that day, the stock less than 3% above the previous close (no chasing a move that far), and a Nifty 50 not down more than 0.2%. A sell is the mirror image. Recent news, insider trades or deals pointing the other way withhold the call; they never create one. So does a close more than 3× the five-minute range from VWAP, or a stock at its NSE price band or with its target beyond it.
- **Levels.** The stop is the larger of 1.5× the recent five-minute true range and 0.4% of the price, and no call needs a stop wider than 2%. The target is twice the stop distance. Both are whole exchange ticks. A call expires ten minutes after its candle closes.
- **Only calls your settings would trade.** Each setup is priced as the order your trading settings would place: amount per trade, maximum loss, minimum reward-to-risk after charges (the automatic minimum where it applies), at the order levels below. One that fails reads wait, with the reason, and is not listed, alerted, placed or recorded. A call shows its order: shares, limit, stop, target, and the net rupees at the target and at the stop.
- **Telegram.** Each new buy or sell call is sent once, with that order; a stock called again later in the day gets a new message to approve, or a note that it already has an order that day.
- **Orders.** Live trading starts off. Once an administrator saves a trading token and switches it on, a call can be approved on the page, or by replying APPROVED to its Telegram message, until the call expires ten minutes after its candle; a later scan in the opposite direction, or with news against it, withdraws it. The server rechecks the call and the live price, then places one Upstox GTT order: a limit entry 1% below the call's price for a buy (1% above for a sell), a stop 1% beyond that limit, and the call's target; the entry is cancelled if the price does not come back that far before the call expires. Quantity is capped by both the amount per trade and the loss at the stop, and an order whose estimated charges leave the target earning less than 1.5× what the stop loses (configurable) is refused. Limits per trade and per day are set on the page. A stock is ordered at most once a day; a call the broker refused can be approved again. An unclear broker response is never retried automatically.
- **Automatic orders.** A separate switch, off by default, places calls whose volume candle traded more than 50× the usual for that time without an approval, within the same limits and at the same order levels. An automatic order must earn at least 2.5× what its stop loses, after charges (a separate setting from approved calls' 1.5×); a call that falls short is skipped. A refused or unclear automatic order is not retried that day.
- **Net P&L.** When an order closes at its target or stop, the app reads its entry and exit fills from Upstox and records the P&L after brokerage, STT, fees, stamp duty and GST (estimated at the fills). It is sent on Telegram and shown in the orders table, with today's and the last 30 days' totals. The Upstox contract note is final.
- **Paper record.** Each call is replayed the next day on that session's candles: fill, stop or target first, or a time exit at 15:15, in units of the stop distance, and in rupees after charges at the current amount per trade and maximum loss. The page shows the record by volume-jump band, including 50× and over, and by how strongly the volume candle closed. That shows which rules have worked before any money depends on them.
- **Backtest.** `igs intraday-backtest --from DATE` replays the rules on Upstox's public five-minute history. On 300 stocks from July to early October 2026 they lost money after charges: −0.20 R a trade before these changes, −0.16 R after them, with over half the calls stopped out. A reversal fade and an opening-range breakout, tested the same way, lost too ([details](docs/INTRADAY.md#backtest)). Keep automatic placement off and judge changes with the backtest and the paper record.
- **Not modelled:** slippage, and the dynamic bands of derivatives stocks. Strength is "Technical" or "Supported", not a probability.

## Getting started

**[docs/DEPLOY.md](docs/DEPLOY.md)** is a step-by-step guide to installing and running the app on a Windows or Ubuntu desktop: PostgreSQL, settings, the first data load with realistic timings, the daily schedule (systemd timer or Task Scheduler), backups and troubleshooting. In short:

```bash
uv sync --all-groups                        # Python 3.12; the 'ui' group adds Streamlit
createdb igs                                # PostgreSQL 16
echo "IGS_DATABASE_URL=postgresql://igs:igs@localhost:5432/igs" > .env
uv run igs db migrate

# 1. Prove every endpoint returns real data (needs network access to NSE/BSE)
uv run igs sources verify && uv run igs sources list

# 2. Load history
uv run igs ingest static nse_equity_list nse_trading_holidays bse_scrip_master angel_scrip_master
uv run igs ingest prices --start 2014-01-01 --end 2026-09-22
uv run igs ingest range nse_corporate_actions --start 2014-01-01 --end 2026-12-31
uv run igs ingest range nse_announcements --start 2024-01-01 --end 2026-09-22
uv run igs ingest range nse_insider_trading --start 2024-01-01 --end 2026-05-02
uv run igs ingest range nse_insider_disclosures --start 2026-04-25 --end 2026-09-22
uv run igs master rebuild
uv run igs ingest symbols nse_quote_equity          # industry classification (else announcement labels; each check adds 25)
uv run igs ingest static nse_financial_results_index nse_shareholding_index
uv run igs ingest pages nse_integrated_filing_index --backfill --max-pages 1400
uv run igs ingest documents financial_results
uv run igs ingest documents shareholding
uv run igs ingest documents insider_trading

# 3. Check it before trusting it
uv run igs recon --start 2024-01-01 --end 2026-09-22
uv run igs validate fundamentals

# 4. Gate, backtest, score
uv run igs gate run
uv run igs backtest --start 2016-01-01 --end 2025-06-30
uv run igs score

# 5. Look at it
uv run igs ui          # http://localhost:8501 (this computer only; --host to change)
uv run igs api         # http://localhost:8000/docs
uv run igs db status   # what is loaded: rows per table, latest price day and filing
```

For daily use, schedule `scripts/igs-job.sh daily` (or `scripts\igs-job.cmd daily` on Windows) after the evening bhavcopy, `... sync --trigger timer` every 30 minutes to pick up new filings while the UI is closed, and `... sources verify` weekly: `scripts/crontab.example` and docs/DEPLOY.md show how. `igs ui --no-sync` starts the UI without its own checks. Screener.in exports you downloaded are imported with `igs import screener FILE... [--nse SYMBOL]` (or on the stock page). They check exchange figures and fill gaps for the AI. Exports whose reporting basis is verified also fill scoring gaps from the time they were verified: background downloads by the page they came from, uploads by their figures agreeing with the app's consolidated or standalone results filings. `igs import yfinance` loads fallback prices that are flagged as unverified.

Settings are environment variables. `igs` also reads them from a `.env` file in the repository folder (`IGS_ENV_FILE` points elsewhere); a variable already set in the environment wins.
- `IGS_DATABASE_URL`, `IGS_RAW_ROOT` (default `data/raw`), `IGS_CONFIG_DIR`, `IGS_GATE_PATH`, `IGS_IC_STATUS`.
- Email alerts: `IGS_SMTP_HOST/PORT/USER/PASSWORD`, `IGS_ALERT_FROM`, `IGS_ALERT_TO`.
- Telegram alerts (the Settings page writes these): `IGS_TELEGRAM_TOKEN`, `IGS_TELEGRAM_CHAT_ID`.
- WhatsApp messages for the AI's buy and sell calls (the Settings page writes these): `IGS_WHATSAPP_PROVIDER` (`meta` or `callmebot`), `IGS_WHATSAPP_TO`, then `IGS_WHATSAPP_TOKEN` and `IGS_WHATSAPP_PHONE_ID` for Meta, or `IGS_CALLMEBOT_APIKEY`.
- Research assistant (optional): `ANTHROPIC_API_KEY` (the UI's Settings page writes it to `.env`), and `IGS_SETTINGS_DIR` for where the page keeps changed settings (default `data/settings`).
- Sign-in: `IGS_AUTH_MODE` (`oidc` by default, `local` for a desktop-only install) and `IGS_API_TOKEN` for the API ([docs/PUBLIC_HOSTING.md](docs/PUBLIC_HOSTING.md)).
- Intraday calls (optional): `UPSTOX_ACCESS_TOKEN` for candles and `UPSTOX_TRADING_TOKEN` for approved orders; the Intraday page writes both ([docs/INTRADAY.md](docs/INTRADAY.md)).
- Screener.in background exports (optional): `SCREENER_EMAIL` and `SCREENER_PASSWORD`, written by `igs screener configure` ([docs/SCREENER_BACKFILL.md](docs/SCREENER_BACKFILL.md)).
- Telegram messages about the app's own failures: `IGS_OPERATIONAL_ALERTS=1` ([docs/OPERATIONAL_NOTIFICATIONS.md](docs/OPERATIONAL_NOTIFICATIONS.md)).

Moving to another server with all the data: [docs/MIGRATION.md](docs/MIGRATION.md). Upgrading an existing install (shared ingestion lock, stored-document recovery): [docs/RELIABILITY_UPGRADE.md](docs/RELIABILITY_UPGRADE.md).

## Configuration

| File | What it controls |
|---|---|
| `universe.yaml` | NSE mainboard (EQ, BE), market cap > ₹500 cr, at least 8 quarters filed as of the date. SME and ASM/GSM excluded unless enabled. Buckets use the SEBI method (rank by 6-month average market cap: large = top 100, mid = 101–250, small = 251+). Sector and industry filters. |
| `scoring.yaml` | Pillar weights Momentum 20 / Quality 20 / Valuation 20 / Low volatility 20 / Growth 15 / Ownership 5, with the evidence for them. Factor weights within a pillar (equal unless stated; 0 = tracked, not scored). Enabled factors, valuation modules, winsorisation, peer groups, tier cut-offs, whether IC status is respected. Robustness gates, plausibility bounds per factor, run-health limits. The market-mood tilt of the pillar weights and the stock sentiment overlay (`sentiment`), and the geopolitical overlay. |
| `backtest.yaml` | Monthly rebalance plus a quarterly sensitivity run, horizons 3/6/12 months, deciles, benchmark, IC gate (t ≥ 3 on non-overlapping observations), walk-forward selection. The failure definition and the check-effectiveness population. |
| `costs.yaml` | Statutory charges, brokerage, impact model, assumed capital. **Re-check the rates against current circulars.** |
| `red_flags.yaml` | 25 checks, each with a severity (reject or caution) and thresholds. Governance: pledge > 20% of promoter holding; promoter holding down > 5 pp over two quarters; at least 2 primary issues adding up to more than 10% of shares in 3 years; ASM/GSM; contingent liabilities > net worth; resignations; audit qualification. Earnings quality: receivable days > 1.5× the 3-year median; other income > 25% of PBT; profits not converting to cash; accruals; Altman Z''; Piotroski; Beneish; cash-and-debt paradox; exceptional items; restatements. Data integrity: unit-scale jumps, statement identities, results overdue (with SEBI deadline extensions). Market: liquidity, volatility, run-up, drawdown, trade-for-trade. |
| `sources.yaml` | Every endpoint, its tier, format and session handling; UDiFF final-session IDs; allowed hosts for XBRL documents. |
| `xbrl_concepts.yaml` | SEBI in-capmkt element → concept mapping per taxonomy version; shareholding axes and members. |
| `hand_checked.yaml` | The 20 validation companies (bank, NBFC, two EMS firms, two commodity cyclicals, …). The values are left for a person to type in. |
| `alerts.yaml` | Alert rules and channels (email, Telegram, WhatsApp), including open-market insider trades on watchlist names and which AI calls go to WhatsApp. |
| `sync.yaml` | Checking NSE for new files: the interval while the UI is open (2 h), whether to check when it starts, the minimum gap between checks (60 min), when today's price files are asked for (after 19:00 IST) and the document limit per check. |
| `broker_calls.yaml` | Where brokers' calls are read from (the Economic Times stock-news feeds), how long they count, and which stocks they bring into the AI's calls. |
| `news.yaml` | Feeds for the geopolitical news feature. A feed with nothing new for 14 days is reported as stopped. |
| `assistant.yaml` | The optional research assistant: on/off, Claude model, refusal fallbacks, daily budget, per-feature effort and limits, token prices for the budget estimate. Values changed on the UI's Settings page override it from `data/settings/assistant.yaml`. |

## Known limitations

- **Unvalidated weights.** No backtest has been run on real data yet, so every weight, tier cut-off and threshold is a prior taken from published evidence. The UI says so on every run until an IC report exists. With history accumulating only from 2025, the first IC report will also cover a single market regime.
- **Data rights.** NSE's website terms of use forbid systematic or automated data collection without NSE's express written consent. This app collects its main data automatically from nseindia.com, with requests spaced 5 s apart and a browser-like client, which lowers the load but is not consent. Before relying on it, ask NSE for consent or use a licensed data feed; this is the user's decision and is not settled in the code.
- **Parsers not yet confirmed on real data.** The results parsers have been run on real NSE instances; the shareholding XBRL parser has not. The verification gate and schema fingerprints make a format mismatch fail loudly rather than load wrong numbers.
- **Missing history.** Historical index membership, historical ASM/GSM stages and the four-level industry classification before 2023 are not available from current files. Surveillance filtering in backtests is limited to dates covered by landed snapshots. Industry labels before they were first observed use the earliest known label.
- **Annual-report-only data.** Some red-flag inputs (contingent liabilities, audit opinion) may only be in annual reports. Until they are loaded, those flags report *data unavailable*; contingent liabilities is configured not to block High conviction meanwhile, and is shown as not checked.
- **Failure rates describe the past.** They are measured under exactly the production rules, with confidence intervals, but a future period can be worse than any in the backtest. With few High conviction name-dates the interval is wide; read its upper end.
- **Published models on Indian data.** The Altman Z'' and Beneish M-score coefficients were estimated on non-Indian companies and their inputs are mapped to Ind AS lines (proxies are documented in the code). They are cautions, and the check-effectiveness table is what should decide whether they stay.
- **Costs and market cap in the backtest.** One current schedule of statutory charges is applied to the whole backtest. Market cap uses the share count from the latest shareholding filing known at each date. A company without one gets it from paid-up equity capital divided by face value in its latest results filing, accepted only where it agrees with profit divided by basic EPS in the same filing. The backtest universe is empty before the first filing that states either.

## Development

```bash
uv run ruff check src tests
IGS_TEST_DATABASE_URL=postgresql://igs:igs@localhost:5432/igs_test uv run pytest
uv run pytest -m lookahead        # the gate on its own
```

- **Database tests** need a disposable database whose name contains "test", because each test drops and recreates the schema. Without `IGS_TEST_DATABASE_URL` they are skipped, and pytest ends with a red line saying how many; CI runs them all.
- **Before pushing:** `uv run python scripts/ci_local.py` runs what CI runs, database tests included, after checking that this copy has every commit already on the remote branch. Two writers share the branch; [AGENTS.md](AGENTS.md) has the rules, and `git config core.hooksPath .githooks` makes `git push` run the same check.
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
  factors/              registry and 36 factors (growth, quality, valuation, momentum, low volatility, ownership)
  score/                normalisation, composite, checks (governance, accounting, integrity,
                        market), robustness, plausibility, run health, tiers, explanations,
                        persistence
  backtest/             calendars, engine, costs, metrics, failure measurement, report
  recon/                reconciliation checks and report
  alerts/               rules and delivery
  assistant/            optional research assistant on the Claude API: ask (read-only tool
                        loop), briefs, announcement notes; never imported by scoring code
  api/  ui/             FastAPI app, Streamlit app and charts
  universe.py service.py sync.py daily.py cli.py
tests/
```

### Growth research workflow

See [the growth roadmap](docs/GROWTH_ROADMAP.md) for implemented measures and the
remaining data/validation requirements. The additional growth, bank and volume
measures are unweighted research evidence; they do not upgrade production ratings.

```bash
uv run igs research audit
uv run igs research backfill --limit 25
uv run igs research extract --limit 5
uv run igs research validate --start 2025-01-01 --end 2026-09-30
```

PDF extraction requires explicit opt-in (`IGS_FORWARD_AI_ENABLED=true`), an enabled
Claude assistant and `pdftotext`. It accepts public NSE/BSE HTTPS PDFs only. The daily
pipeline runs bounded extraction and a coverage audit automatically. Historical
comparisons are exploratory and cannot substitute for prospective validation.

### Models by task

Settings supports separate Anthropic, OpenAI, Google Gemini, DeepSeek and OpenRouter
models for each AI task. Provider model catalogs refresh every six hours; new models
need confirmed pricing before assignment. Existing Claude defaults are preserved.
See [configuration and deployment](docs/MODEL_ROUTING.md).
