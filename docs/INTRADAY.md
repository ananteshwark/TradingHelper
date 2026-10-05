# Intraday calls

The **Intraday calls** page is separate from fundamental rankings and their quarterly
eligibility rules. It reads persisted Upstox scans; opening the page does not call an LLM
or submit orders. Viewers can read it. Only administrators can change credentials or
verify investor classifications and enable approved trading.

## Activate Upstox

1. For market-data scanning, generate an Upstox Analytics or OAuth token. For order
   placement, generate a **standard trading OAuth access token** from a trading-enabled
   developer app using the [official authentication flow](https://upstox.com/developer/api-documentation/authentication/).
2. Sign in to the app as administrator. Open **Intraday calls → Upstox connection**.
3. Save the data token as `UPSTOX_ACCESS_TOKEN` and the trading token as
   `UPSTOX_TRADING_TOKEN` through the two password fields. Both stay in the private
   server `.env`, never Git or the database. The Analytics token is read-only and cannot
   place orders. Upstox's standard OAuth trading token expires at 03:30 IST the next day;
   renew it through Upstox and replace it in the administrator page.
4. The next scheduled scan checks the connection during market hours. An expired/refused
   token stops the scan and produces a visible error, plus the existing operational alert.

### Connect using your Algo API key and secret

In **Intraday calls → Upstox connection → Connect your Upstox Algo app**, enter the
key and secret privately, together with the exact redirect URL registered in your
Upstox developer app. You can register `https://stocks.ednis.ai/` for this site.
Choose **Prepare Upstox login**, then **Sign in on Upstox**. Keep the original tab
open. Complete your mobile/OTP/PIN login yourself on Upstox in the new tab. Copy
the complete returned URL from that tab into the original tab's password field and
choose **Finish connection and save trading token** within ten minutes.

The server checks the returned URL and random login state before exchanging the
single-use code. API credentials stay only in the administrator session until the
attempt is consumed, cleared or expired; only `UPSTOX_TRADING_TOKEN` is saved to the
private server settings. Connection does not enable live trading or submit an order.
Review the configured amounts and live setting separately. Repeat when the token
expires. Never send the OTP, API secret, returned URL or access token in chat.

The integration uses official V3 [intraday candles](https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/)
and [historical candles](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/).
Instrument keys use the current NSE equity ISIN. Approved orders use Upstox's
[multi-leg GTT API](https://upstox.com/developer/api-documentation/place-gtt-order/).
An Upstox trading-enabled registered app and any applicable static-IP permissions are
required. Register the server's static IP with Upstox before submitting orders.

## Approve an intraday order

Live trading starts **disabled**. In **Intraday calls → Approved Upstox trading**, the
administrator can enable it after saving a trading token and choosing the per-trade
amount, maximum trades per day, and maximum daily gross order value. The app has no
fixed upper ceilings for those settings; each configured value must be positive.
Broker and exchange quantity, margin, and price restrictions still apply. Short SELL entries
are allowed with Upstox intraday product `I`. The stock can be ordered only once per IST
trading day after a submitted or uncertain outcome. A definite broker rejection can be
approved again after the credential or order problem is fixed.

Only the administrator can approve an active call on the page. Alternatively, reply
**APPROVED** to that exact call message in the configured *private* Telegram chat. The
bot checks the reply's message ID and sender/chat ID. Forwarded messages, group replies,
ordinary messages saying approved, expired alerts, and withdrawn calls do not place orders.
The latest scan must still confirm the same call. The trading worker checks replies and
broker status every 20 seconds with `igs-intraday-approvals.timer`.

At approval, the server reads Upstox's live price and rejects the order if it moved over
0.5% from the call reference or crossed the stop/target. Quantity is sized from the
administrator's per-trade amount. The approved entry is an immediate **limit** order at the live price, so
acceptance does not guarantee a fill. The same GTT request attaches the call's stop-loss
and target. The worker cancels an unfilled entry when the call expires; a filled entry
retains its protective exits. Upstox order and position status remains the source of truth.

The trade record is reserved before the broker request. If the request times out or the
response is unclear, the app marks it **uncertain**, alerts Telegram, and will **not**
resubmit automatically. Check the Upstox GTT/order book before taking any action.
An explicit Upstox 4xx refusal is recorded as **rejected**, does not use a daily trade
slot, and can be retried after correction. Unfilled entries that expire also release
their daily count and gross-value budget. A read-only-token refusal also disables live
trading until a proper OAuth token is saved and trading is re-enabled.
No order is placed merely because a call was identified or sent.

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
Commands: `uv run igs intraday --limit 100`, `uv run igs intraday-deals`,
`uv run igs intraday-approvals`.
Logs: `logs/intraday.log`, `logs/intraday-deals.log`,
`logs/intraday-approvals.log`.
Services: `igs-intraday.service`, `igs-intraday-deals.service`,
`igs-intraday-approvals.service`.
The existing one-minute Telegram dispatcher watches both services and reports newly loaded
investor events and history-cache batches. It also sends separate **INTRADAY BUY/SELL**
messages with symbol, reference, stop, target, volume jump, momentum, supporting evidence
and expiry. Only a completed latest scan with a candle no older than five minutes can
send. Waiting, failed, expired and overnight setups are excluded.

One alert is delivered per stock/direction/IST trading day, so continuing signals are not
repeated each scan; an opposite-direction call can send separately. Failed sends retry
after one minute while the latest scan still confirms the setup. A withdrawn call is
discarded, not sent late after recovery. Delivery is at-least-once: a crash after Telegram
accepts but before the database acknowledgement may repeat a message. Alerts use the
existing private Telegram bot/chat configuration and `igs-notify.timer`.

`intraday_scan` stores scan status; `intraday_signal` stores prices, rule readings,
timestamps, evidence and version. History cache expires after 35 days; scan snapshots
remain. No historical backtest or proven profitability is asserted by these rules.
