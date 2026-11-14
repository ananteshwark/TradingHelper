# Local GPU opening-candle study (13 November 2026)

The owner asked whether real-time candle movement and its volume, alongside prior-day
stock and market inputs, can improve intraday predictions. The [protocol](PROTOCOL.md)
was fingerprinted before the full fit. This is a local research engine, not a source
of server calls or orders.

## Data and method

- Downloaded public Upstox five-minute candles through 12 November 2026 for the
  Nifty 50 and 197 current Nifty 200 stocks, with zero reported fetch failures.
  The files are ignored under `data/intraday-gpu/candles/` on the owner's computer.
- The existing feature builder yielded 205,807 complete stock-days on 1,092 dates.
  The new builder added 29 opening-candle and volume predictors, plus later entry,
  exit and outcome fields. Predictors end with the 09:40 candle; simulated entry is
  the 09:50 open and exit is the 15:00 open.
- Both models use the same GPU XGBoost settings, 2022-2024 training dates and fixed
  0.15% predicted-move gate. The baseline uses 24 prior stock/market features;
  the expanded model uses 53. The report confirms `cuda:0` on the local RTX 3050.
- Each date can take at most three buys and three shorts. Results below include
  Upstox charges and 0.02% slippage on both entry and exit, on ₹1 lakh positions,
  with zero-call days included. This is a simulated fill, not a broker record.

| Model | Year | Mean date rank correlation | Trades | Net ₹/day | Daily t |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 2025 | +0.0377 | 768 | +306 | 1.25 |
| Candle + volume | 2025 | +0.0356 | 820 | +464 | 1.79 |
| Baseline | 2026 | −0.0029 | 709 | −380 | −1.39 |
| Candle + volume | 2026 | −0.0041 | 745 | −542 | −1.86 |

The volume accompanying rising opening candles had a small negative mean
cross-sectional rank correlation with the later move: −0.0072 on training dates,
−0.0120 in 2025 and −0.0190 in 2026. Correlation of a candle's absolute move with
its volume was near zero in 2025 (−0.0012) and +0.0128 in 2026. These are daily
cross-sectional averages, so a market-wide rise does not create the relationship.
They are weak, unstable predictors, not a reliable stand-alone trading rule.

At 0.05% slippage per side, the expanded model's net result fell to +₹269/day in
2025 and −₹749/day in 2026. At 0.10% it lost in both years. The added features
did not improve the recent period and do not meet the app's after-charges goal.
The 2025-2026 data were already examined in earlier studies, and using today's
Nifty 200 members creates survivorship bias. Neither period is an independent
holdout. The model must not replace the current server model on this evidence.

The expanded-minus-baseline comparison on the **same dates** was +₹158/day in
2025 (paired daily t 0.68) and −₹162/day in 2026 (t −0.65) at 0.02% slippage
per side. Neither difference is reliable. The report also records daily buy and
short results and prediction deciles. Both model variants show poor calibration
of predicted versus realised returns in the recent period, so their raw output
must not be read as a probability or a dependable price target.

## Data-quality sensitivity

A full comparison of the old research features with the production feature code
rebuilt 205,784 stock-days and found 506 differing rows. All 23 invalid OHLC
candles in these stock files were the 09:15 bars of 25 June 2024. Production
rejects each affected day, while the old research builder included it; each of
the following 22 sessions then uses a different history window. The
[sensitivity plan](SENSITIVITY.md) was fingerprinted before rerunning the GPU
fit. It conservatively excluded these 23 invalid dates and their next 22
stock-sessions: 529 feature rows, of which 524 passed the model's liquidity
filter. Evaluation dates and their rows stayed identical.

| Model after exclusion | 2025 net ₹/day | 2026 net ₹/day |
| --- | ---: | ---: |
| Baseline | +126 | −140 |
| Candle + volume | +252 | −522 |

The expanded-minus-baseline paired difference became +₹125/day in 2025
(t 0.54) and **−₹382/day in 2026** (t −1.50). That is a substantial change
from removing under 0.5% of the training rows, consistent with the older
LightGBM study's seed sensitivity. It strengthens the decision not to deploy
this model. The exclusion is a stress check, not a reconstructed production
training set or new holdout. Results and exact data/code hashes are in
`data/intraday-gpu/quality-experiment/report.json` on this computer.

## Reproduce locally

The fetchers are resumable. The initial files were downloaded with two workers of
`research/ml/fetch_candles.py`, then extended after both finished:

```bash
uv run python research/candle_gpu/extend.py data/intraday-gpu/candles 2026-10-09 2026-11-12 0 2
uv run python research/candle_gpu/extend.py data/intraday-gpu/candles 2026-10-09 2026-11-12 1 2
uv run python research/ml/intraday_features.py data/intraday-gpu/candles data/intraday-gpu/base-full.parquet
uv run python research/candle_gpu/build.py data/intraday-gpu/candles data/intraday-gpu/base-full.parquet data/intraday-gpu/augmented-full.parquet
uv run --with xgboost-cu12==3.4.1 python research/candle_gpu/train.py data/intraday-gpu/augmented-full.parquet data/intraday-gpu/new-experiment
uv run python research/candle_gpu/quality_stress.py data/intraday-gpu/candles data/intraday-gpu/augmented-full.parquet data/intraday-gpu/augmented-quality.parquet
uv run --with xgboost-cu12==3.4.1 python research/candle_gpu/train.py data/intraday-gpu/augmented-quality.parquet data/intraday-gpu/new-quality-experiment
```

Use a new output directory for each training run. The saved
`data/intraday-gpu/full-experiment-audit3/report.json` includes the data and code
hashes, date-level model predictions and net results, prediction deciles, paired
daily comparisons, feature correlations, slippage stresses, and the trained models.
Future work needs a frozen prospective paper comparison, along
with verified point-in-time coverage for any additional fundamental or sector
inputs, before changing future calls.
