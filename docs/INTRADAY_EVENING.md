# Evening intraday picks (paper)

The page **Trading → Evening picks** and Telegram track, forward and on paper, the model
from Study D ([research/evening](../research/evening/README.md)). It picks the next
session's intraday trades the evening before, so they are known before the open. Nothing
here places an order.

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

## Installing

`scripts/install-schedules.sh` installs and enables the timer with the others. Apply
migration `072_intraday_evening.sql` first (the deploy script does).
