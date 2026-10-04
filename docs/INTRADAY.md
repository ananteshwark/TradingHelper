# Intraday calls

The **Intraday calls** page is separate from fundamental rankings and their quarterly
eligibility rules. It reads persisted Upstox scans; opening the page does not call an LLM
or submit orders. Viewers can read it. Only administrators can change credentials or
verify investor classifications.

## Activate Upstox

1. Generate an access token in your own Upstox developer account using its
   [official authentication flow](https://upstox.com/developer/api-documentation/authentication/).
2. Sign in to the app as administrator. Open **Intraday calls → Upstox connection**.
3. Paste the token into the password field and save. It is written to the private server
   `.env` as `UPSTOX_ACCESS_TOKEN`, never to Git or the database. Renew it here when expired.
4. The next scheduled scan checks the connection during market hours. An expired/refused
   token stops the scan and produces a visible error, plus the existing operational alert.

The integration uses official V3 [intraday candles](https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/)
and [historical candles](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/).
Instrument keys use the current NSE equity ISIN. No trading/order APIs are used.

## Signals and timing

- Scheduled every five minutes. Entry window: weekdays 09:30–15:15 IST; six closed candles
  are required, so the first eligible setup is at 09:45. Fresh current-session bars are
  mandatory even on weekdays, withholding calls on holidays or feed outages.
- Up to 100 active NSE equities: recent broker/AI calls or investor disclosures first,
  then fundamental score. Liquidity filtering applies regardless of score.
- Five-minute candle volume must be at least 1.8× the median for the **same time bucket**
  over up to 20 previous sessions, with at least five sessions available. Historical
  candles are fetched once per stock/day over the preceding 28 calendar days.
- Buy: positive 15-minute momentum ≥0.3%, close above session VWAP and the first 15-minute
  high, and Nifty 50 session change ≥−0.2%. Sell is the corresponding inverse.
- Require ₹1 crore session turnover and ₹10 lakh latest-candle turnover. Stop distance
  is max(1.5× intraday true-range average, 0.4% of reference price); no setup with a stop
  wider than 2%. Target is twice that risk distance. All are reference levels, not fills.
- No active call on incomplete/stale data, missing benchmark or volume history, opposing
  recent evidence, failed/unfinished scans, after expiry, or outside the entry window.
- Calls expire ten minutes after their candle closes, capped at 15:15. Five-minute
  freshness is required when creating them. No new position is implied after expiry.
- Strength is **Technical** or **Supported**, not a calibrated win probability.
  Execution spread/slippage, exchange price bands, broker short-sale restrictions and
  position sizing are not modeled. SELL describes a bearish setup, not proof of borrow
  availability. Recheck the broker quote and execution constraints before using levels.

The initial historical-cache warm-up can take several scans. Each scan stops after its
bounded runtime; the page reports the number actually checked, not the requested count.

## Investor and news context

Existing AI-assessed stock news and geopolitical news are linked by company and shown
with source links. Only assessments available by the scan can influence it; confidence
below 0.7 does not influence direction. Recent opposing evidence withholds a technical
setup. Supportive context alone cannot create a call.

Existing NSE insider open-market purchases/sales are included with disclosure and trade
dates. NSE's public `bulk.csv` and `block.csv` are collected hourly from 08:10–20:10 IST
on weekdays. Raw CSV snapshots are retained in `intraday_deal_fetch`; purchases and sales
by the same investor/stock/date within each report are netted. Unmapped symbols are not
attached to guessed companies. No publication time is invented: the collector's first
observation is used conservatively and the original trade date remains visible.

**Prominent investors and institutions → verified names** lets an administrator classify
exact disclosed names as prominent, FII or DII. Until verified, a name is shown as an
unclassified large trade and does not affect direction. No default guesses based on
names or FII/DII aggregate flows. Administrators can also record a verified public
disclosure with its URL, evidence, trade date and publication time.

These are disclosed transactions, often published after trading, not a live view of
institutional orders. Quarterly shareholding changes are not treated as today's buys.
Context covers the last three days of publication/first observation; absence is displayed
as unavailable evidence, not proof that no buying/news exists.

## Operations

After migration, install timers with `scripts/install-schedules.sh` as the app user.
Commands: `uv run igs intraday --limit 100`, `uv run igs intraday-deals`.
Logs: `logs/intraday.log`, `logs/intraday-deals.log`.
Services: `igs-intraday.service`, `igs-intraday-deals.service`.
The existing operational Telegram monitor watches both services and reports newly loaded
investor events and history-cache batches. This feature does not send a Telegram message
for every five-minute setup.

`intraday_scan` stores scan status; `intraday_signal` stores prices, rule readings,
timestamps, evidence and version. History cache expires after 35 days; scan snapshots
remain. No historical backtest or proven profitability is asserted by these rules.
