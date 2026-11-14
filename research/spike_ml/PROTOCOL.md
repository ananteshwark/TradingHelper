# Spike recovery with machine learning: protocol

Written on 29 October 2026, before any model below was trained. Its SHA-256 is in
`PROTOCOL.sha256`. The gate is chosen on the validation year and written in `CHOICE.md`
before the test period is run, once.

## Question

The owner asked: use machine learning to find by what percentage a stock has moved when
it recovers the most, and how big that recovery is; the same logic and percentage would
then be used for intraday trades.

Two parts:

1. **Descriptive:** after a move of X% within 30 minutes, how far does the price come back
   at most before 15:15? Broken down by X.
2. **The model:** at each point where a stock's move within 30 minutes first reaches 5%,
   6%, 7%, 8%, 10%, 12% or 15%, predict the after-cost result of trading against it with a
   target of 1–5%. Trade at the first point whose best prediction clears a gate, with that
   target. Does this make money after costs where fixed rules didn't?

## Already known before writing this

From `research/spike_reversal` and `research/spike_sizes`, on the same stocks and periods:

- **Bigger moves gave back 3% first more often** in 2022–2024 (48% against 17% at 15%),
  less so in 2025–2026 (35% against 31%).
- **Fixed rules failed:** the best of 2022–2024 (fade a 15% move at once, 3% target, no
  stop) lost 0.24% a trade in 2025–2026.
- **The 2025–2026 test period below** was the confirmation period of both studies. No model
  or rule here has been run on it, but the owner's idea has been tested there. A pass would
  still need the forward paper record.

## Data

- **The spike-sizes candidate days** (`research/spike_sizes`): every NSE EQ stock-day of
  2022 – 8 October 2026 with a range of 5% or more and a median traded value of Rs 2 crore
  over the previous 20 sessions, with Upstox's public 5-minute candles.
- **Re-prepared here with volume** (`prepare.py`). The same days are used: full sessions,
  with a range within 0.5% of the bhavcopy's.
- **The Nifty 50's 5-minute candles** (Upstox, `research/seq/fetch.py`) for the market.
- **NSE's bhavcopies** for each stock's previous close and the previous session's return.

## Decision points

- **One for each threshold:** for each stock-day and each X in {5, 6, 7, 8, 10, 12, 15}%,
  the first candle, from 09:15 to 14:55, whose move reaches X.
- **The move** is defined as in `research/spike_sizes`:
  - close / the lowest low of the candles starting from 25 minutes before, less 1 (a rise);
  - or close / their highest high, less 1 (a fall).
- **The trade** is against the move, entered at the next candle's open. That candle must
  start before 15:15.

## Outcomes (labels)

- **Maximum recovery:** the largest move back against the spike from the entry, before
  costs. For a rise: (entry − the lowest low) / entry, over the candles from the entry
  candle to the last one before 15:15. For a fall, mirrored with the highest high.
- **Its timing:** minutes from the entry to the candle that made it.
- **For each target T in {1, 2, 3, 4, 5}%:** the trade's net result as in
  `research/spike_sizes`:
  - Rs 1 lakh a trade, Upstox intraday charges;
  - 0.15% slippage a side, none on the target;
  - no stop, out at the open of the 15:15 candle.

## Features

All are known at the decision candle's close:

- **The move:**
  - its size (|move| %) and side;
  - its speed: minutes from the window's extreme to the decision candle.
- **When:** minutes since 09:15.
- **The day so far:**
  - the close against the day's open and against the previous close;
  - the opening gap;
  - the day's range so far;
  - how far the close is from the day's extreme on the move's side.
- **The decision candle:**
  - its range %;
  - its body / range;
  - where its close sits in its range;
  - the returns of the three candles before it.
- **Volume:**
  - the decision candle's volume against the day's earlier average;
  - the value traded so far today against the 20-session median.
- **The stock:** the log of the 20-session median value; the log of the price; the
  previous session's return.
- **The market:**
  - the Nifty 50's return from its open to the decision time, and its move over the last
    30 minutes;
  - how many decision points at 5% the market had before this one that day, in all and on
    the same side.

## Model

- **LightGBM regressors** (native API), one for each T, on the decision points of the
  training years, with fixed settings:
  - L2 objective, learning rate 0.03, 400 rounds;
  - 15 leaves, at least 200 rows a leaf;
  - feature and bagging fractions 0.8 (bagging every round), L2 penalty 1.0;
  - seed 7.
- **One more regressor** for the maximum recovery, with the same settings, for the
  descriptive part. Its accuracy is reported; it isn't used to trade.

## Walk-forward

- **Validation:** train on 2022–2023, predict 2024. The gate is chosen here.
- **Test:** train on 2022–2024, predict 1 January 2025 – 8 October 2026, once, with the
  chosen gate.

## The trading rule

- For each stock-day, its decision points are taken in time order.
- The first whose best prediction across T exceeds the gate g is traded, with the T that
  gave it. One trade a stock a day.
- **Gates:** g in {0, 0.25, 0.5, 0.75, 1.0}% of the trade.

## Measure, choice and pass rule

The measure is the daily net P&L in rupees over every session of the period, zero on a day
without a trade.

- **The choice:** the gate with the highest validation (2024) t.
- **It passes** with all of:
  - a validation t of at least 2.5;
  - then a test mean above zero with t ≥ 2.0;
  - and at least 100 test trades.

If it doesn't pass, nothing is built into the app. If it passes, it goes into the app as a
paper track first, not as orders.

## Also reported (descriptive, not tests)

- **The direct answer, for each X in both periods:**
  - the mean and median maximum recovery;
  - how often it reached 1%, 2%, 3%, 4% and 5%;
  - the median minutes to it.
- **The recovery model's accuracy:** the correlation between predicted and actual
  recovery, and the actual recovery by decile of prediction.
- **The features' importance** (gain).
- **Fixed rules on the same points, for comparison:** each X with each T, entered at once,
  no stop.
- **The test trades** broken down:
  - by the target chosen and by X;
  - rises and falls separately;
  - by liquidity, and today's Nifty 200;
  - locked exits, the busiest days, and 0.05% and 0.30% slippage.

## Limits known in advance

- As in the earlier studies:
  - brokers' intraday restrictions (ASM and GSM) aren't replayed;
  - spreads are assumed;
  - price bands can lock an exit.
- **No past price bands:** NSE's past band lists aren't here, so the model can't use the
  paper track's band rule. The move from the previous close stands in for it.
- **Decision points within a stock-day are related,** and so are a crash day's many
  spikes. The daily measure counts each day once.
