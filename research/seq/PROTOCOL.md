# Study C: a sequence model on raw 5-minute candles against Study B's LightGBM

Written 2026-10-25, before any candle was downloaded for this study or any sequence model
was fitted. Its SHA-256 is in `PROTOCOL.sha256`. Anything changed after that is an
amendment, dated and placed at the end, before the result it could affect.

## Question

Study B (`research/ml/`) predicts each liquid Nifty 200 stock's move from 09:45 to 15:15
with LightGBM on 23 hand-made features. Does a neural network that reads the raw 5-minute
candles directly predict that move better, on the same days, stocks, costs and trading
rules?

## What is already known (stated so it can't be used quietly)

The Study B holdout (January 2025 to 8 October 2026) has been looked at before: Study B's
chosen model passed it, almost all of its profit came from short sells, and 2026 was
weaker than 2025. This study reuses that holdout so the two models can be compared on
identical days. Because it has been seen, a pass here is weaker evidence than a first
look. Nothing below was chosen to fit those results: the trading rules, candidates, costs
and selection rule are Study B's, unchanged. A passing model still needs a forward paper
test before it is used.

## Data

Study B's: Upstox public 5-minute candles (`research/ml/fetch_candles.py`) for the 197
stocks in `research/ml/universe_nifty200.json` and the Nifty 50, from December 2021 to
24 October 2026, downloaded again for this study (`fetch.py`: Study B's fetcher with that
end date). Days after 8 October are used only for the post-holdout report. A day counts when its first candle is
09:15 and it has at least 70 candles. Rows, entry, exit, target, liquidity filter (Rs 50
crore a day over 20 days) and the clipped target (+/-5%) are exactly Study B's, rebuilt
from the new download with `research/ml/intraday_features.py`. Only rows Study B keeps are
used, so both models see the same stock-days.

## The sequence model's inputs

For each stock and day, at 09:45, a sequence of 81 five-minute steps: the last full
session before today (75 candles, 09:15 to 15:25) and today's first six (09:15 to 09:40).
A missing candle in that window drops the row (and its Study B row, so the sets match).

Each step has 12 channels, computed only from candles closed by 09:45:

- the stock's open, high, low and close as log ratios to the previous session's close;
- its log volume less the log of that candle-time's mean volume over the previous 20
  sessions (zero when that mean is missing or zero);
- the same five for the Nifty 50 at the same timestamp (volume channel zero, as the index
  has none);
- the step's time of day, scaled to [0, 1]; and a flag, 1 for today's candles.

Values are clipped to +/-0.2 (log ratios) and +/-5 (volume).

Two candidate models:

- **seq**: the sequence alone.
- **hybrid**: the sequence plus Study B's 23 features (standardised on the training rows
  and clipped at +/-5 standard deviations, as Study B's ridge; missing values zero).

## Network and training (fixed now)

- A 1-D convolutional stem (two layers, 64 channels, kernel 5, GELU), a single-layer GRU
  with 64 units reading the steps in time order, and a two-layer head (64 units, GELU,
  dropout 0.1) on the GRU's last state, concatenated with the 23 features for hybrid.
- Loss: mean squared error on the clipped target. AdamW, learning rate 1e-3 with cosine
  decay to zero, weight decay 1e-4, batch 1,024, 6 epochs, bf16 autocast on the GPU.
- No early stopping and no validation split: the epochs are fixed.
- Each prediction is the mean of three networks trained with seeds 1, 2 and 3.
- **Walk-forward:** as Study B, retrained every quarter on all earlier days, first model
  January 2023. Development 2023-2024; holdout January 2025 to 8 October 2026.

## Trading rule candidates and choice

Study B's, unchanged: each day buy up to k stocks with the highest predictions above zero
and sell short up to k with the lowest below zero; k = 3 or 10; every such stock, or only
those predicted to move at least 0.15%. Slippage 0.02% a side and Upstox intraday charges
on Rs 1 lakh (`igs.intraday.costs`), entry at the 09:45 open, exit at the 15:15 open.
{seq, hybrid} x {3, 10} x {no gate, gate}: eight candidates, evaluated with Study B's own
`trades` and `stats` functions.

**Chosen:** the highest t-statistic of daily net P&L in 2023-2024, written to `CHOICE.md`
with its development results before the holdout is computed. A tie goes to seq, then
k = 3, then gate on.

**Baseline:** Study B's chosen candidate (LightGBM, k = 3, gate on), refitted with Study
B's code on the rebuilt rows, reported on development and holdout alongside. Daily rank
correlation of predictions with the target is reported for both models.

## Pass (holdout, chosen candidate only, computed once)

1. Study B's own bar: mean daily net P&L above zero with t >= 2.0; profit factor >= 1.1;
   positive in 2025 and in 2026 to date; at least 100 trades.
2. Better than the baseline: the mean of the daily difference in net P&L (chosen minus
   LightGBM, days with no trades counting zero) above zero with t >= 2.0.

Passing 1 but not 2 is reported as "works, not better than LightGBM": the app keeps its
current model. Passing both makes the sequence model a candidate for forward paper calls
alongside the current one, not a replacement.

## Reported, not required

Long and short sides separately; each year; results from 9 to 24 October 2026 (after the
holdout, a few sessions, too few to judge); and the seeds' spread.
