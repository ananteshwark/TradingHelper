# Detailed spike input study — 14 November 2026

The frozen [protocol](PROTOCOL.md) was run on this machine's NVIDIA GPU. **Do not
promote the rich model to live or paper calls.** It lost more after simulated
charges than the 24-feature GPU control in both evaluation years. This does not
contradict the recent positive forward paper record: the historical sample is
today's Nifty 200 constituents, uses a different date range, and has
survivorship bias. Neither result establishes reliable future profit.

## Data and trained artifacts

- Source: 197 currently listed Nifty 200 stocks and Nifty 50 public five-minute
  Upstox candle files through 12 November 2026. A complete, valid 09:15–15:25
  session, 20 earlier accepted sessions, and ₹2 crore median daily turnover
  were required. A 5% day range was required, which every actual 5% spike
  event already satisfies by its decision time.
- 20,557 eligible stock days, 6,243 spike threshold events, 4,532 distinct
  decision candles. There are 24 incumbent and 76 new features. New fields
  include six completed candle shapes and relative volumes, signed volume,
  price/volume correlations, VWAP distance, prior-session activity, 5/20-day
  stock context, and Nifty context through the decision candle. Future stock
  and market bars are excluded from the inputs by a mutation test.
- Training dates: 2022–2024, with 3,298 distinct decisions. Evaluation used
  1,234 decisions: 248 Nifty sessions in 2025 and 213 in
  2026. All arms used the same decisions, simulated outcome labels, and 0.5%
  predicted-net gate. Five target regressors were fit per arm. GPU XGBoost
  3.4.1 reported `cuda:0`, and the trainer rejects a CPU fallback.
- Local generated data, saved models, trade lists, hashes, and full metrics:
  `data/spike-rich-v1/` and `data/spike-rich-v1/models/report.json`. This
  directory is ignored by Git because the source candles and models are large.
  The manifest fingerprints the input data and training code.

## Results after configured charges and 0.15% simulated slippage

Each trade assumes ₹1 lakh notional. Cash days are included in daily averages.
The original LightGBM settings were refit on this **same subset**, so this is
not a comparison with the deployed all-liquid-NSE artifact.

| Year | Arm | Trades | Net ₹ | Mean ₹ / session | Daily t | Profit factor |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2025 | LightGBM, 24 inputs | 191 | −128,982 | −520 | −1.489 | 0.578 |
| 2025 | GPU XGBoost, 24 inputs | 181 | −130,395 | −526 | −1.275 | 0.552 |
| 2025 | GPU XGBoost, 100 inputs | 172 | −148,873 | −600 | −1.358 | 0.483 |
| 2026 | LightGBM, 24 inputs | 141 | −81,643 | −383 | −1.239 | 0.674 |
| 2026 | GPU XGBoost, 24 inputs | 116 | −9,298 | −44 | −0.118 | 0.952 |
| 2026 | GPU XGBoost, 100 inputs | 94 | −48,194 | −226 | −0.734 | 0.726 |

The rich inputs reduced net profit relative to the identical GPU learner by
₹18,477 in 2025 and ₹38,896 in 2026. The paired daily difference t values
were −0.563 and −1.105; these are not strong evidence of a stable difference,
but the new candidate also never met the basic requirement of positive net
profit. A chronological first-ten-trades-per-day cap kept the rich model
negative (−₹79,577 and −₹67,949). At 0.05% slippage it lost ₹118,666 and
₹35,573; at 0.30% it lost ₹193,501 and ₹66,404.

Short-side losses were ₹111,751 in 2025 and ₹32,074 in 2026 for the rich arm.
The worst common dates were 7 April 2025 and 24 September 2026. The 2026 rich
arm had 17 exits on candles with no price range, a fill reliability warning.
Nifty context, prior stock return, VWAP distance, and recent candle/volume
fields appeared among the fitted model's highest gain features. Feature gain
describes a fitted tree; it does **not** show profitable differentiation.

## Interpretation and next use

The detailed inflow is implemented and tested, but the available historical
subset does not support replacing `spike-ml-v1`. The 2025–26 periods have also
been seen in earlier research, so these comparisons are exploratory. The
current-member universe omits delisted names and the original all-liquid-NSE
training files were unavailable here. No historical price-band, order-book,
futures, institutional ownership, or filing data was available for these
decision times; none was substituted from later data.

The reused historical simulator applies the slippage to the entry and time
exit, but its target-hit exit is priced at the target without a separate exit
slippage deduction. Thus its target-hit result can be optimistic, despite the
protocol's shorthand “entry/exit slippage.” These are simulated fills, not
executed trades. Charges use the repository's configured rates. The negative
finding is strong enough to block promotion without correcting that
optimism, since correction would only make target-hit results worse.

For the next study, restore a dated all-liquid-NSE universe with delisted
stocks and historical band availability, then freeze a new protocol and
collect subsequent shadow calls with actual fills and costs. Compare the rich
and incumbent models on those **same forward dates** before considering any
server-side change.

## Reproduce locally

From the repository root, with local candle files in
`data/intraday-gpu/candles/`:

```bash
sha256sum -c research/spike_rich_gpu/PROTOCOL.sha256
uv run python research/spike_rich_gpu/build_days.py data/intraday-gpu/candles data/spike-rich-v1
uv run python research/spike_ml/events.py data/spike-rich-v1 data/intraday-gpu/candles/NSE_INDEX_Nifty_50.json data/spike-rich-v1/events
uv run python research/spike_rich_gpu/enrich.py data/spike-rich-v1 data/spike-rich-v1/events data/intraday-gpu/candles/NSE_INDEX_Nifty_50.json data/spike-rich-v1/rich-events.parquet
uv run --with xgboost-cu12==3.4.1 --with lightgbm python research/spike_rich_gpu/train.py data/spike-rich-v1/rich-events.parquet data/spike-rich-v1/calendar.json data/spike-rich-v1/models
```

The commands refuse to overwrite prior output directories and trained models.
Use a fresh output directory when repeating a run.
