# Detailed spike-model data study — 14 November 2026

Written before fitting a model or inspecting results from this new feature set.
Fingerprint in `PROTOCOL.sha256`. This is a local research comparison, never an
automatic replacement for `spike-ml-v1` or a source of live orders.

## Question and sample

Can richer information **available when a 5-minute spike candle closes** improve
the after-cost result of fading that spike, beyond the incumbent model's 24
inputs? Use the locally downloaded public Upstox five-minute candles for 197
**current** Nifty 200 stocks and the Nifty 50, 2022–12 November 2026. This is a
different, narrower universe than the incumbent all-liquid-NSE study and has
survivorship bias. No missing historical band, order-book, futures, ownership or
filing data will be filled using today's values.

Accept only a stock session with valid OHLC/volume at every bar, a 09:15 bar, a
15:25 bar, and the expected five-minute sequence through 15:25. Derive its
previous close, previous-session return, 20-session median traded value and
same-time historical volume from **earlier accepted stock sessions only**.
Exclude a day until 20 earlier sessions exist; require median value ≥₹2 crore
and a current-day high/low range ≥5%. Keep the Nifty's valid session dates as
the evaluation calendar, including days with no selected trades.

## Decision and outcomes

Reuse `research/spike_ml/events.py` unchanged for the decision points and costed
labels: the first 5-minute candle at which a stock reaches each of 5, 6, 7, 8,
10, 12 or 15% inside a 30-minute window, no later than 14:55; enter at the next
candle's open and fade the move. Distinct decisions on the same candle count
once. For target 1–5%, use its ₹1 lakh trade outcome after configured Upstox
charges and **0.15% entry/exit slippage**; no stop, target or 15:15 time exit.
Stress the saved 0.05% and 0.30% slippage outcomes. Missed/unavailable entries
are excluded rather than assigned a zero profit. These are simulated fills.

## Detailed inflow at each decision

Keep the incumbent 24 `f_` fields. Add only causally available fields:

- Last six completed stock candles: returns, ranges, signed bodies, wick and
  close position, VWAP distance, volume by direction, concentration, burst
  versus the preceding bars and versus the same 5-minute slot in 20 prior
  sessions; price/volume relationship and realised volatility.
- Previous accepted stock sessions: last day's return, gap, range, close
  position, final-hour move and relative final-half-hour volume; five- and
  twenty-session returns, 14-session ATR/price, 20-session volatility and
  median traded value. Preserve missingness instead of replacing an unknown
  previous return with a measured zero.
- Nifty 50: prior-session and five-session returns, today's gap, returns over
  the latest 5, 15, 30 and 60 minutes known by this candle, plus stock move
  relative to that market move. Existing same-direction spike crowd counts
  include only earlier decisions in this sampled universe.

Entry/exit prices, later high/low, eventual recovery and any bar after the
decision candle are **labels only**. A mutation test must show that changing
future bars cannot change any candidate feature.

## Fixed models and evaluation

Train once on 2022–2024. Report 2025 and 2026 separately on identical event
rows, dates, outcome labels and trades for three arms:

1. Refit the incumbent LightGBM settings on this subset with its 24 features.
2. XGBoost with those same 24 features, to separate learner from data detail.
3. The same GPU XGBoost settings with the added fields, to isolate their value.

Fit one regressor per target (1–5%) to the simulated net percentage at 0.15%
slippage. XGBoost uses `reg:squarederror`, `device=cuda`, `tree_method=hist`,
300 trees, depth 4, learning rate 0.03, min child weight 30, max bin 128,
subsample/column sample 0.8, L2 10, seed 1729, four CPU threads. It must report
`cuda:0`, not silently run on CPU. The LightGBM arm uses the incumbent settings
from `research/spike_ml/model.py`. No parameter or gate is selected on the
evaluation years. Use the incumbent **0.5% predicted net gate**: first
qualifying decision for a stock-day, with the target whose forecast is highest.

Report daily net rupees on every Nifty session including cash days, mean/day,
daily t, profit factor, trades, buy/short sides, worst date, dates with 20+
trades, and paired daily net difference between each candidate and the first
arm. Repeat with a chronological first-ten-trades-per-day cap as a rough
capacity stress, without using future signals to choose trades. Report the
0.05% and 0.30% slippage stresses without refitting. Save data, feature-code,
training-code and model hashes, predictions, and the trained models.

2025–26 were used in earlier studies, so these are **exploratory comparisons**,
not fresh holdouts. A positive result here can justify a frozen shadow paper
track on later dates; it cannot justify changing live orders or the server's
current spike picks. Call out sparse samples, locked exits, slippage, absent
historical price bands and the current-member universe.
