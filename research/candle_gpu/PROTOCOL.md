# Local GPU candle and volume study

## Question

Does the shape of the opening five-minute candles, particularly the volume accompanying
up and down moves, improve a stock's remaining intraday return prediction beyond the
existing price, prior-session and Nifty 50 inputs? A more complex model is useful only
if it improves decisions after charges and remains stable across dates and slippage.

## Data and timing

- Use the same historical Upstox five-minute Nifty 200 stock and Nifty 50 files as the
  established intraday study. Record missing months and exclude incomplete days. The
  current-member universe has survivorship bias.
- A decision is made after the 09:40 candle closes. Features use no bar later than 09:40,
  plus previous sessions and the Nifty 50 observed by then. The outcome is the stock's
  09:50 open to 15:00 open return, matching the real-order timing more closely than the
  original paper target. Missing entry or exit bars are unknown outcomes.
- The existing 23 numeric features and weekday indicators are the baseline. Added
  features summarize the six opening candles: signed volume, up/down volume ratio,
  price-volume correlation, candle-body and wick structure, VWAP displacement,
  volume concentration, early-to-late acceleration, and stock movement relative to
  the Nifty 50. Every ratio handles a zero denominator as missing.
- Historical score and fundamental fields may be added only where point-in-time
  coverage and publication times can be verified. They will be evaluated as a separate
  ablation rather than silently shrinking the training universe.

## Evaluation

- Compare baseline features and augmented features with the same fixed GPU XGBoost
  configuration and the existing 0.15% predicted-move threshold. Fit on 2022-2024,
  check 2025, and report 2026
  separately. These years were examined in earlier studies, so this is an exploratory
  comparison, not an independent holdout.
- Report rank correlation by day and year, calibration, and the added features' rank
  correlations before and after controlling for market direction. Do not treat stock
  rows from the same date as independent observations.
- Simulate at most three buys and three shorts per day, only predictions beyond the
  fixed threshold. Include zero-call days, 0.02% slippage each side and the
  repository's Upstox intraday charges; stress 0.05% and 0.10% slippage.
- Save the exact feature list, package version, seed, split, training device, data
  hashes, model, predictions, and date-level results. Confirm the model actually used
  CUDA. No server calls or orders change as a result of this study.

Independent forward paper outcomes under a frozen rule are required before any
replacement of the current model. A training run alone makes no accuracy claim.
