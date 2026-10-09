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
amount, maximum trades per day, maximum daily gross order value, maximum loss per trade
at the stop, and minimum reward-to-risk after charges. The app has no fixed upper
ceilings for those settings; each amount must be positive. The maximum loss starts at
1% of the amount per trade (₹100 on ₹10,000), the reward-to-risk at 1.5.
Broker and exchange quantity, margin, and price restrictions still apply. Short SELL entries
are allowed with Upstox intraday product `I`. The stock can be ordered only once per IST
trading day after a submitted or uncertain outcome. A definite broker rejection can be
approved again after the credential or order problem is fixed.

Only the administrator can approve an active call on the page. Alternatively, reply
**APPROVED** to that exact call message in the configured *private* Telegram chat. The
bot checks the reply's message ID and sender/chat ID. Forwarded messages, group replies,
ordinary messages saying approved, expired alerts, and withdrawn calls do not place orders.
APPROVED, APPROVE or "Approved." from the configured chat always gets an answer: the order
status, why it was declined, a request to reply to the call message itself, or that the
message replied to is no longer an open call. A reply that fails unexpectedly is reported
and not retried, so it cannot hold up later replies. Messages from anyone else get no
answer. The 20-second worker runs automatic placement, approvals and order
reconciliation independently: a failure in one is reported and the others still run.
Each run is logged in `logs/intraday-approvals.log`.

**Order levels.** Every order, approved or automatic, is placed away from the call:

- The entry limit is 1% below the call's price for a BUY and 1% above it for a SELL.
- The stop is 1% below that limit for a BUY and 1% above it for a SELL, in place of the
  call's stop. The target stays the call's.

A call at ₹100 with a ₹102 target is an order at ₹99, stop ₹98.01, target ₹102. Each level
is rounded to the tick away from the call's price (the stop away from the entry), so
neither step is less than 1%. The entry fills only if the price comes back 1% before the
call expires, and is cancelled otherwise; in the backtest below, that happened on about 5%
of calls. A BUY that fills has usually traded below where the call put its stop (above it
for a SELL), as the call's stop is 0.4% to 2% away; the paper record measures calls at
their own levels. An order whose stop the live price has already reached is refused.
A separate administrator switch, **Automatically place calls above 50× volume**, allows
an open BUY or SELL call to place an order without an approval reply when its latest
five-minute candle traded strictly more than 50 times the median volume for that same
five-minute slot in prior sessions. The exact stored volumes determine this threshold;
the displayed ratio is rounded. The switch starts disabled on new installations and
requires live trading plus a current trading token. The 20-second approval worker
checks for such calls after each completed scan. It uses the same broker eligibility,
freshness, linked exits, per-trade loss and value limits, charge-adjusted reward-to-risk
check, daily limits and order levels as manual approval, with one difference: the target,
net of charges, must earn at least 2.5× what the stop loses with charges (*Minimum
reward-to-risk after charges, automatic orders*; approved calls keep their own minimum).
How far the call's target is decides it: recommended ₹100 with a ₹102 target is an order
at ₹99, stop ₹98.01, target ₹102, 3.03× before charges, 2.17× after them at ₹10,000 per
trade (skipped; an approved call clears its 1.5×) and 2.72× at ₹1,00,000 (placed). A call
skipped this way places no order. An automatic order records
`auto` as the order source, sends a separate order-status message, and never retries a
recorded broker rejection or uncertain submission for that stock on that day. An
eligible alert says automatic placement may be attempted; Upstox acceptance and fill
are confirmed separately. The administrator can turn off this switch at any time.
A call can be approved until its stated expiry, ten minutes after its candle closes.
Later scans do not cut that short when the stock merely reads *wait* (a later candle
without a fresh volume jump), or while the next scan is still running. A later completed
scan does withdraw the call if it shows the opposite direction, or news or disclosures
against it. The trading worker checks replies and
broker status every 20 seconds with `igs-intraday-approvals.timer`.

At approval, the server reads Upstox's live price and rejects the order if it moved over
the configured deviation from the call reference or crossed the stop/target.

**Size.** Quantity is the smaller of the amount per trade divided by the recommended
price, and the maximum loss per trade divided by the distance to the stop. A 2% stop
therefore buys half as many shares as a 1% stop for the same loss.

**Charges.** Before the order, the server estimates the round-trip charges: brokerage on
both orders, STT on the sell, exchange and SEBI fees, stamp duty and GST, at the rates in
`config/costs.yaml` (`intraday`). The target, less its charges, must earn at least the
minimum reward-to-risk times what the stop would lose plus its charges; otherwise the
order is refused and the message gives both figures. Brokerage capped per order makes
small tickets expensive. A BUY call at ₹101.35 with a ₹102.25 target is an order at
₹100.33, stop ₹99.32: at ₹10,000, about ₹27 of charges leave the target earning 1.28× what
the stop loses, so it is refused at the default 1.5×. At ₹1,00,000 the same call clears it
(about ₹83 of charges, 1.68×). The trade record and
the Telegram confirmation show the loss at the stop and the estimated charges.
The entry is an immediate **limit** order at the entry level above, 1% better than the
**recommended reference price**. Every order price is rounded to the stock's tick, the tick
fetched again at approval: a buy limit is never above 99% of the call's price and a sell
limit never below 101%.
A buy may fill at that price or lower; a sell at that price or higher. The limit never
chases the live quote and acceptance does not guarantee a fill. This uses Upstox's documented
GTT `IMMEDIATE` limit-order semantics: https://upstox.com/developer/api-documentation/place-gtt-order/.
The same GTT request attaches the stop-loss (1% beyond the limit)
and the call's target. The worker cancels an unfilled entry when the call expires; a filled entry
retains its protective exits. Upstox order and position status remains the source of truth.

**Net P&L.** When a trade closes at its target or stop, the worker reads the orders the
GTT placed (the `order_id` of its ENTRY rule and of the exit rule that completed, from
[GTT order details](https://upstox.com/developer/api-documentation/get-gtt-order-details/))
and each order's average fill and filled quantity from
[order details](https://upstox.com/developer/api-documentation/get-order-details/). The net
P&L is the move between the fills times the quantity, less brokerage, STT, exchange and
SEBI fees, stamp duty and GST estimated at the fills with the `intraday` rates in
`config/costs.yaml`. It is stored on the trade (gross, charges, net), sent on Telegram, and
shown in the orders table with today's and the last 30 days' totals. The Upstox contract
note is final. A trade closed some other way (by hand, or by the broker's square-off), or
whose entry and exit fills differ in quantity, gets a note and an operations issue instead
of a figure. An Upstox failure is retried five minutes later, for three days.

The trade record is reserved before the broker request. If the request times out or the
response is unclear, the app marks it **uncertain**, alerts Telegram, and will **not**
resubmit automatically. Check the Upstox GTT/order book before taking any action.
An explicit Upstox 4xx refusal is recorded as **rejected**, does not use a daily trade
slot, and can be retried after correction. Unfilled entries that expire also release
their daily count and gross-value budget. A read-only-token refusal also disables live
trading until a proper OAuth token is saved and trading is re-enabled.
No order is placed merely because a call was identified or sent.

## Broker intraday eligibility

Scans select only normal NSE equities in Upstox's official MIS instrument list and
exclude its suspended instruments. Filtering happens before the top-100 candidate
limit, preserving priority for AI/broker calls and higher scores among eligible stocks.
The public lists are refreshed at least every five minutes when needed; yesterday's
or expired snapshots are not used if a refresh fails. The page and Telegram delivery
recheck eligibility, and each approval forces a new list download before any quote
or order request. A stock removed by Upstox is withheld even if an older alert exists.
Broker margin, account-specific restrictions and changing exchange rules can still
reject an otherwise eligible order; list inclusion does not guarantee acceptance.

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
- Require ₹10 crore traded in the session so far and ₹10 lakh in the latest candle (₹1
  crore before `intraday-v4`: thin stocks' same-time medians are tiny, so their volume
  jumps are noise, and they lost more in the backtest). Stop distance
  is max(1.5× intraday true-range average, 0.4% of reference price); no setup with a stop
  wider than 2%. Target is twice that risk distance. All are reference levels, not fills.
- Stop and target are whole exchange ticks. Each stock's tick (₹0.01 to ₹5 by price band)
  comes from the same Upstox MIS list. A buy rounds both down and a sell rounds both up,
  so the stop moves slightly away from the entry and the target slightly toward it.
  A call whose stop or target would land on the entry at that tick is withheld.
- No call once the stock is 3% or more from the previous session's close in the call's
  direction (`intraday-v4`): calls made 3–4% into the day's move were the worst in the
  backtest, and moves that far tend to reverse rather than extend. A buy on a stock that
  fell 3% and is now turning up is not a chase.
- No chasing: no call when the close is more than 3× the five-minute true range from
  VWAP (the stop is 1.5×), so a return to VWAP would cost about twice the stop. The 3×
  is a starting point, not fitted.
- Price bands: NSE's security list (`sec_list.csv`, downloaded once a day) gives each
  stock's band, 2% to 40% of the previous close. The previous close is the official
  close of the last session in the loaded bhavcopy. No call is made at the band, where
  orders queue unfilled, or with a target beyond it. Derivatives stocks ("No Band")
  have dynamic bands instead and are not checked. If the list cannot be downloaded, a
  stock is not in it, or a banded stock has no previous close in the last 7 days,
  calls are withheld.
- No active call on incomplete/stale data, missing benchmark or volume history, opposing
  recent evidence, failed/unfinished scans, after expiry, or outside the entry window.
- Calls expire ten minutes after their candle closes, capped at 15:15. Five-minute
  freshness is required when creating them. No new position is implied after expiry.
- Strength is **Technical** or **Supported**, not a calibrated win probability.
  Execution spread and slippage, derivatives stocks' dynamic bands and broker short-sale
  restrictions are not modeled; size and charges are applied at the order (see "Approve
  an intraday order"). SELL describes a bearish setup, not proof of borrow
  availability. Recheck the broker quote and execution constraints before using levels.

The initial historical-cache warm-up can take several scans. Each scan stops after its
bounded runtime; the page reports the number actually checked, not the requested count.

**Only calls your settings would trade.** Each scan prices every buy/sell setup as the
order that would be placed for it (`trading.order_check`): the amount per trade, the
maximum loss at the stop and the minimum reward-to-risk after charges, at the order levels
(entry 1% away, stop 1% beyond it, the call's target); a setup above 50× volume while
automatic placement is on is held to the automatic minimum. A setup that fails reads
*wait*, with the reason, and is not a call: it is not listed as active, alerted on
Telegram, placed automatically or kept in the paper record. A call shows and alerts the
order itself: shares, limit, stop, target, and the net rupees at the target and at the
stop after estimated charges.

## Backtest

`uv run igs intraday-backtest --from 2026-07-01 [--to DATE] [--stocks 300] [--seed N]`
replays the rules on a seeded sample of Upstox MIS-eligible equities. It downloads their
five-minute candles and the Nifty 50's from Upstox's public historical endpoint (no
token; cached under `data/intraday/backtest/`), and runs `engine.evaluate` on every closed
candle as the scanner would. The first call of each stock and day is resolved as the
paper record resolves calls, in R after the charges on a ₹1 lakh position. News is not
replayed, and the trading-settings check is not applied.

Results on 300 stocks, 1 July to 7 October 2026 (run on 8 October 2026):

| Rules | Calls | Stop first | Target first | Win rate | Average R after charges, Jul–Aug / Sep–Oct |
|---|---|---|---|---|---|
| `intraday-v3` (before) | 8,695 | 58% | 23% | 34% | −0.205 / −0.183 |
| `intraday-v4` (no chase, ₹10 crore) | 3,680 | 56% | 23% | 36% | −0.170 / −0.154 |

- Calls 3–4% into the day's move were the worst bucket under `intraday-v3`. Calls above
  50× volume lost more than any other volume band (−0.28 R), mostly thin stocks.
- An entry 1% better than the call (now every order's) filled on about 5% of calls within
  their ten minutes, and those trades lost too.
- Three alternatives, fixed before their results were seen, also lost after charges in the
  later period: fading a stock 3% or more into its move on a reversal candle (−0.21 R); a
  five-minute opening-range breakout on the day's highest-volume openers with a stop at
  10% of the daily range (−0.91 R), and with the stop at the other end of the opening
  candle (+0.05 R in Jul–Aug, −0.21 R in Sep–Oct).
- At ₹1 lakh a round trip costs about ₹80 in charges, about 0.16 R at a 0.5% stop. A rule
  has to win more than that before it earns anything.

`intraday-v4` loses less, but no rule tested made money after charges. Treat calls as
candidates to review, keep automatic placement off, and judge any rule change with this
backtest and the paper record before money depends on it.

## Paper record

Every buy and sell call is replayed the next day on that session's five-minute candles,
from the first later scan's cached history of the stock (`igs.intraday.outcomes`, table
`intraday_call_outcome`):

- The limit entry fills if a candle starting before the call expires trades at or through
  the recommended price; otherwise the call is *not filled*.
- After the fill, the first of stop and target to be touched decides it. A candle that
  touches both counts as the stop, and so does the stop in the fill candle itself: the
  order within a candle is unknown, so the worse case is taken.
- Neither by 15:15 IST, when brokers square off intraday positions, is a *time exit* at the
  close of the last candle before it.
- The result is in R, units of the stop distance, before charges and slippage.

The Intraday page shows the last 60 days by group: all calls, volume-jump bands (1.8–5×,
5–20×, 20–50×, 50× and over), and whether the volume candle closed in the top 30% of its
range (toward the trade). Each group gives fills, target first, stop first, time exits, win
rate, average and total R. It answers whether a rule such as "only 50× volume" or "only
strong closes" did better than the rest, before any money depends on it. A handful of calls
proves nothing either way. No call and no order reads the record.

Each group, and each of the latest 50 calls, also shows rupees after charges: a filled call
is sized as an order would be with the current amount per trade and maximum loss (the
smaller quantity), and its result is less brokerage, STT, fees, stamp duty and GST
estimated as for an order. Slippage is not included. Changing the trade settings changes
these figures for past calls too.

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
Each `igs intraday` run first does the ML paper calls' work that is due: storing the
previous sessions before 09:45, the day's calls between 09:45 and 09:55, the results from
15:20 ([INTRADAY_ML.md](INTRADAY_ML.md)). Its output line starts `ML intraday:`.
The existing one-minute Telegram dispatcher watches both services and reports newly loaded
investor events and history-cache batches. It also sends separate **INTRADAY BUY/SELL**
messages with symbol, reference, stop, target, volume jump, momentum, supporting evidence
and expiry. Only a completed latest scan with a candle no older than five minutes can
send. Waiting, failed, expired and overnight setups are excluded.

One alert is delivered per call (stock, direction and candle), so a call is not repeated
by later scans, while a stock called again on a later candle gets a new message that can be
approved by replying to it. If the stock already has an order that day, the message says
another can't be placed instead of asking for approval. Failed sends retry
after one minute while the latest scan still confirms the setup. A withdrawn call is
discarded, not sent late after recovery. Delivery is at-least-once: a crash after Telegram
accepts but before the database acknowledgement may repeat a message. Alerts use the
existing private Telegram bot/chat configuration and `igs-notify.timer`.

`intraday_scan` stores scan status; `intraday_signal` stores prices, rule readings,
timestamps, evidence and version. History cache expires after 35 days; scan snapshots
remain. The rules lost money after charges in the backtest above; no profitability is
claimed.
