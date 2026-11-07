# Evening intraday picks

The page **Trading → Evening picks** and Telegram track, forward and on paper, the model
from Study D ([research/evening](../research/evening/README.md)). It picks the next
session's intraday trades the evening before, so they are known before the open. When the
owner turns on real orders, each call can also be approved for a real Upstox order (see
[Real orders](#real-orders)); nothing is ordered without an approval.

## Why this model

The owner wanted the stocks known much earlier than the 09:45 model allows. Study D was
fixed in advance (protocol, choice, one holdout run):

- **Decision** after the close, from what is known by then: the last day's move, its last
  30 minutes and its volume, the close against the day's VWAP, the 5- and 21-day trend,
  volatility, distance from the 20-day high and low, and the Nifty 50's own moves.
- **Model:** LightGBM, five seeds averaged so one random draw can't decide the result.
- **Trades:** up to three buys above +0.15% and three short sells below −0.15%. Entry at
  the next session's 09:15 open, exit at its 15:15 open.
- **Costs:** 0.05% slippage a side (the open is the least liquid moment) and Upstox
  intraday charges on ₹1 lakh a call.

**Holdout (January 2025 to 8 October 2026):** 1,540 calls, +0.14% a call after costs,
profit factor 1.16, ₹488 a day (t 2.02, just over the bar of 2.0); 2025 ₹646 a day,
2026 ₹283. Every seed alone was profitable (₹361 to ₹684 a day).

**Its weaknesses:**

- It passed by a hair, on a period earlier studies had already looked at.
- All the profit came from short sells; buys lost 0.04% a call.
- The universe is today's Nifty 200, a bias that favours buys, not the shorts it profits on.
- Real orders at the open can fill worse than the 09:15 candle's open.
- In the 10 sessions after the holdout it lost ₹2,160 a day, mostly on buys.

A forward record is the honest test. Judge it after a few hundred calls, not a few weeks.

## How it runs

- **Evening (`igs-intraday-evening.timer`, weekdays 15:45 to 21:00):** once today's Nifty
  50 session is complete, it fetches each Nifty 200 stock's last 45 days of 5-minute
  candles from Upstox (about three requests a stock, a few minutes in all), computes the
  features, scores the liquid ones and records the calls in `evening_pick`. A Telegram note
  lists them. It runs once an evening; later runs only retry if the data wasn't complete.
- **Next session, from 15:20 (`igs-intraday.timer`):** each call is closed from that
  session's candles: entry at the 09:15 candle's open, exit at the 15:15 candle's open,
  with the result after costs, and a Telegram note of the day and the total.
- A stock is skipped when Upstox returns bad candles for it, or when its last 22 sessions
  don't match the Nifty 50's. A missing session must not shift the features.
- The weekday feature assumes the next weekday. After an exchange holiday the real
  session is later, which the model can't know.

The model is fixed: `config/models/intraday_evening_v1.json` holds its five boosters,
trained on every day to 23 October 2026. The app evaluates them itself, without LightGBM.
`research/evening/export_model.py` rebuilds it and checks the app's predictions match
LightGBM's (they agree to 1e-11). `research/evening/check_features.py` checks the app
computes the same inputs as the research: on 20 stocks it matched on all but the 66 days
after three candles Upstox served malformed on 25 June 2024. Live, such a stock is
skipped that evening.

It needs an Upstox access token (Settings, Upstox tab) and the Nifty 200 list.

## Real orders

When the paper record convinces the owner, each evening call can be traded with real money,
approved one at a time. Nothing is ordered without an approval. There is no automatic
unlock: the switch is the owner's decision, with the paper record shown beside it.

**Switching it on.** In **Settings → Intraday trading**, tick both **Enable live approved
trading** and **Real orders for the evening calls**. A standard Upstox trading OAuth token
must be saved (Settings → Upstox).

**Approving** (from the evening decision until 09:15 IST on the next weekday):
- **Telegram:** with real orders on, each call gets its own message instead of the one
  paper note. Reply APPROVED to the message itself.
- **Page:** on the **Trading** page's **Evening picks** tab, under Real orders, choose the
  call, tick the confirmation and press **Approve**.

An approval only records the decision; nothing reaches Upstox overnight.

**The order** (`igs.intraday.trading.place_evening`, run every 20 seconds by
`igs-intraday-approvals.timer`):
- From **09:20 to 09:30 IST** on the trading day, once the Nifty 50's 09:15 candle shows
  the market has opened (so a holiday can't trigger an order). Each approved call is
  attempted once; a failure is reported and never retried.
- **Refused** when the live price is further from the signal day's close than the trading
  settings' **maximum price deviation**. At the default 0.5% most opening gaps exceed it;
  1% to 2% (the maximum) suits these calls better. The refusal is noted on the call and
  sent by Telegram.
- Otherwise the 09:45 model's order: one Upstox intraday GTT, entry a limit 0.1% past the
  live price, stop half the 14-day ATR away, no target, squared off by the app at 15:00
  IST. The amount per trade, maximum loss, one order a stock a day and daily limits apply.
- An approval lapses if that weekday is an exchange holiday, or once evening or live
  orders are switched off.

**How it differs from the paper record:** entry about 09:20 instead of the 09:15 open,
with a stop and a 15:00 exit instead of none and 15:15, and gaps beyond the limit skipped.
That combination was not backtested; expect real results to differ from the record.

## Installing

`scripts/install-schedules.sh` installs and enables the timer with the others. Apply
migration `072_intraday_evening.sql` first (the deploy script does).
