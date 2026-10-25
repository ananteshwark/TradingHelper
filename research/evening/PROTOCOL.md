# Study D: picking intraday trades the evening before

Written 2026-10-25, before any feature or model for this study was computed. Its SHA-256
is in `PROTOCOL.sha256`. Anything changed later is an amendment, dated, at the end, before
the result it could affect.

## Question

The current intraday model (Study B) decides at 09:45. The owner wants the stocks known
much earlier: the evening before, so orders can be ready for the open. Can a model using
only what is known by the previous day's close pick each liquid Nifty 200 stock's direction
from the 09:15 open to 15:15, profitably after costs?

## What is already known (stated so it can't be used quietly)

The 2025-to-October-2026 period has been seen through Studies B and C: at 09:45 the
signal was mostly the Nifty's first 30 minutes and short sells of stocks that gapped up
after a rise; 2026 was weaker; LightGBM results swing with the random seed. None of that is
available at the evening decision (no gap, no first 30 minutes). The rules below copy
Study B's structure and were not tuned on that period. A pass is still weaker evidence than
a first look and needs a forward paper test.

## Data

Study C's download (`research/seq/fetch.py`): Upstox 5-minute candles for the 197 stocks
in `research/ml/universe_nifty200.json` and the Nifty 50, December 2021 to 24 October
2026. A day counts when its first candle is 09:15 and it has at least 70 candles.

## Decision, entry, exit, costs

- **Decision** after the close of day t-1, using candles up to 15:25 on t-1.
- **Entry** at the open of day t's 09:15 candle; **exit** at the open of its 15:15 candle.
  No stop or target.
- **Costs:** Upstox intraday charges on Rs 1 lakh (`igs.intraday.costs`) and **0.05%
  slippage each side**, more than Study B's 0.02% because the opening minutes are the
  least liquid. 0.02% is reported, not used for the pass.
- Stocks averaging at least Rs 50 crore a day over 20 days, as Study B.
- **Target:** day t's return from entry to exit, clipped at +/-5%.

## Features (all known by 15:30 on t-1)

From the 22 sessions to t-1, exactly as Study B where it has the same feature: the last
day's return, close location, last-hour return and open-to-close return; 5- and 21-day
returns; ATR as % of price; distance from the 20-day high and low; log 20-day traded
value; 5-day sums of overnight and intraday returns. New, from t-1's candles: the last
30 minutes' return (15:00 open to close), its volume over that window's 20-day average,
and the close against the day's VWAP. The Nifty 50's last-day, last-hour, 5- and 21-day
returns and its last 30 minutes. The stock's last-day return less the Nifty's. The day of
the week of day t. 25 features.

## Models

- **ridge** (alpha 1.0, standardised on training rows, clipped at +/-5 sd, missing 0).
- **gbm**: Study B's LightGBM settings (400 trees, learning rate 0.03, 15 leaves, at least
  500 rows a leaf, subsample 0.7, columns 0.7, lambda 5), **averaged over seeds 1-5**, so
  the result doesn't depend on one random draw.
- **Walk-forward:** retrained every quarter on all earlier days, first model January 2023.
  Development 2023-2024; holdout January 2025 to 8 October 2026.

## Candidates and choice

Study B's: each day buy up to k with the highest predictions above zero and short up to k
with the lowest below zero; k = 3 or 10; every such stock, or only those predicted to move
at least 0.15%. {ridge, gbm} x {3, 10} x {no gate, gate}: eight. **Chosen:** the highest
t-statistic of daily net P&L in 2023-2024, written to `CHOICE.md` with its development
results before the holdout is computed. Ties go to ridge, then k = 3, then gate on.

## Pass (holdout, chosen candidate only, computed once)

Study B's bar: mean daily net P&L above zero with t >= 2.0; profit factor >= 1.1; positive
in 2025 and in 2026 to date; at least 100 trades.

## Reported, not required

Daily rank correlation with the target; long and short sides; each year; the result at
0.02% slippage; the five seeds' spread for gbm; 9-23 October 2026 (too few to judge).
