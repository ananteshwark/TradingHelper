# Spike picks, forward on paper: buys after falls and short sales after rises

Written on 29 October 2026, before the track recorded anything. Its SHA-256 is in
`PAPER.sha256`. The track is `igs.intraday.spike_ml`, on the Paper tracking page's Spike
picks tab.

## Why

The study (`README.md`) didn't pass, but its trades split sharply by side:

| | Buys after falls | Short sales after rises |
|---|---|---|
| 2024 | +1.33% a trade, 1,271 trades | +0.16%, 3,647 |
| 2025 – 8 Oct 2026 | +0.60%, 867 (t 2.23) | −0.09%, 3,118 |

That was seen after the fact, so only a forward test can say whether it holds. The owner
asked for:
- both sides as paper tracks;
- an alert whenever the model picks a spike.

## The model

- **Features and settings** exactly as in `PROTOCOL.md`.
- **Trained once** on every decision point from 3 January 2022 to 8 October 2026: five
  regressors, for targets of 1–5%.
- **Fixed from now on:** exported to `config/models/spike_ml_v1.json` (`export_model.py`).
- **Two checks at export:**
  - the app's own feature code reproduces `events.py`'s features on sampled days;
  - the exported trees reproduce LightGBM's predictions.

## Live, every 5 minutes (the intraday job)

**Stocks:** NSE EQ, with a median traded value of at least Rs 2 crore over the previous 20
sessions, from the loaded bhavcopies.

**Data:**
- Upstox's day high, low and last price for each stock.
- Today's 5-minute candles for those whose high is at least 5% above their low. Only closed
  candles count.
- At most 150 stocks' candles a scan, shared with the 12% spike watcher, in this order:
  1. after 15:20, picks waiting to be settled;
  2. stocks in play: new to the list, a new day high or low, or a price move of 1.5% or
     more since the last scan;
  3. the rest, longest unchecked first.
- The Nifty 50's candles, once a scan.

**Decision points and features:** as in `PROTOCOL.md`.
- The previous close and the previous session's return come from the loaded bhavcopy.
- The two crowd features count the 5% decision points found so far today among the stocks
  checked.

**The pick:**
- Taken at the first decision point of a stock's day whose best prediction across the five
  targets exceeds 0.5%, with the target that gave it.
- One a stock a day. Points are judged once, as they are found, and a pick isn't revised.

**Too late to act on:** a pick found more than 10 minutes after its decision candle closed
is recorded as missed, not traded.

**The trade:**
- Entry at the next candle's open, against the move: a buy after a fall, a short sale after
  a rise.
- The target as a limit order, else out at the open of the 15:15 candle. No stop.
- Upstox intraday charges and 0.15% slippage a side (none on the target), Rs 1 lakh a trade.
- No entry candle before 15:15: no trade.

## Two records

- **Buys:** every pick after a fall.
- **Short sales:** every pick after a rise, except when the upper circuit (the price band
  from NSE's list for the day) is less than 4% above the decision candle's close, or isn't
  known. This is the owner's rule from the spike-reversal track. Every short sale is kept
  beside it for comparison.
- **Missed picks** are kept apart, with what they would have made.

## How it will be judged

Each record separately:
- **Too early** until it has 100 trades.
- **Passes** when its daily net P&L, over every session the watcher ran (zero on a day
  without a trade), has a mean above zero and a t-statistic of 2.0 or more.

The buys are the hypothesis; the short sales are their comparison. A pass makes a record a
candidate for real orders, which needs the owner's decision and its own change. Nothing
here places an order.

## Telegram

- **The spikes chat:** each new pick, in one message a scan: the side, the stock, its move,
  the model's expected result and its target. Short sales near the upper circuit are
  listed as not taken.
  - On or off with `model_picks` under `intraday_spikes` in `config/alerts.yaml`.
- **The paper chat:** after 15:20, the day's results and the records.

## Known differences from the study

- **The model has seen more:** it was trained on data that includes the periods where the
  split by side was seen.
- **Busy days:** more than 150 candidate stocks means some are checked a scan late. Their
  picks then count as missed, and the crowd features count fewer stocks than the study did.
- **Live candles** can differ slightly from historical ones.
- **When it decides:** the study ran every point at once; live, a point is judged when it's
  found.
