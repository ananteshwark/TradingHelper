# Adverse moves after the app's recommendations: protocol

Written on 14 November 2026, before any dataset below was built or any outcome computed.
Its SHA-256 is in `PROTOCOL.sha256`. For each study, one candidate per family is chosen on
the development period and written in `CHOICE.md` before the holdout is run, once.

## The question

The owner asked:
- for intensive research to find and train ML models for investing and trading;
- for each recommendation's price to be captured at the moment it is made, and its price
  and volume tracked in real time after it;
- for the parameters responsible for adverse moves to be identified, and the models
  trained accordingly.

Two questions follow, one for trading and one for investing:
1. **Which parameters**, known when a recommendation is made or seen in its price and volume
   soon after, go with an adverse move?
2. **Can a model trained on them** avoid or cut adverse moves enough to earn more after
   costs than the recommendations as they are?

The app's research so far found that a stop-out can't be told from a winner at the moment
of a rule-based call (research/meta_intraday), and that a generic exit model trained on
every stock-day failed (research/exits). This study differs in two ways:
- **Its trades:** the app's two intraday models that passed (the 09:45 calls and the
  evening picks), and the momentum Rankings order.
- **Its models:** trained on trades like the picks, with each feature signed for the
  trade's side, and with the volume path after entry.

The published evidence it draws on:
- **Path labels:** labelling each trade by which barrier its path hits first (stop, target
  or time), and a second model deciding whether to take the first model's pick
  (meta-labelling; López de Prado 2018). Independent out-of-sample support for
  meta-labelling is thin, so it is tested here, not assumed.
- **Momentum crashes:** a stock's nearness to its 52-week high explains them at the stock
  level (Byun & Jeon 2023, building on George & Hwang 2004).
- **Volume:** high-volume winners tend to reverse (Lee & Swaminathan 2000).
- **Lottery stocks:** stocks with an extreme recent daily return underperform (the MAX
  effect; Bali, Cakici & Whitelaw 2011).

## Study A: trading, the two intraday models

### The trades

**Calls:** each model's walk-forward predictions (research/ml for the 09:45 model,
research/evening for the evening model), as the app chooses them. Each day:
- buys: the 3 highest predictions above +0.15%;
- short sells: the 3 lowest below −0.15%.

**The order rule:** the one real orders have (research/ml_live, docs/INTRADAY_EVENING.md):

| | 09:45 calls | Evening picks |
|---|---|---|
| Entry | the 09:50 candle's open | the 09:20 candle's open |
| Stop | half the stock's 14-day ATR from the entry | the same |
| Exit | the 15:00 candle's open | the same |

**Simulation rules:**
- **Stops:** a candle reaching the stop exits at the stop, or at its open when it opens
  beyond it. The entry candle counts.
- **Costs:** Upstox intraday charges on ₹1 lakh a trade, and slippage on the entry and on
  every exit: 0.02% for the 09:45 calls (research/ml_live), 0.05% for the evening picks
  (their own study's, research/evening, since they enter near the open).
- **Not modelled:** the evening order's refusal when the live price is too far from the
  signal day's close (docs/INTRADAY_EVENING.md). This evening rule has not been backtested
  before; F reports it for the first time.

This is the baseline, **F**. Every model below must beat it.

**Data:** Upstox 5-minute candles for the 197 Nifty 200 stocks and the Nifty 50,
January 2022 – 23 October 2026, and the two models' feature tables.

**Periods:**
- **Development:** 2023–2024. The models below are evaluated from 1 July 2023; the first
  six months are their first training data.
- **Holdout:** 1 January 2025 – 23 October 2026.
- **A caveat:** the holdout has been used before, for both models and for research/winners,
  research/exits and research/ml_live. It is a weaker test than a fresh period would be;
  the forward record is the real test.

### What counts as an adverse move

**The label:** a trade is **stopped**, its adverse move, when its stop is hit before
15:00.

Also reported for every trade, in multiples of its ATR and in %:
- its maximum adverse excursion (the worst move against it before the exit);
- its maximum favourable excursion;
- the candle at which each happened.

### Part 1: which parameters go with adverse moves

**Data:** the picks of development and of the holdout, separately.

**The parameters:**
- **At the decision:**
  - every input of the model that made the pick;
  - its prediction, its rank and the trade's side;
  - the day of the week;
  - the Nifty's inputs (gap, return so far, previous day, last 5 days);
  - breadth: the share of the 197 stocks above their previous close at the decision
    candle.
- **After the entry:** at each checkpoint below:
  - the trade's move since entry;
  - its best and worst move so far;
  - its last 15 minutes;
  - its relative volume since entry, and over the last 15 minutes;
  - its distance from the session's VWAP;
  - its position in the day's range;
  - the Nifty's move since entry;
  - the stock's move against the Nifty's.

**Signing for the side:** every return-like or position-like parameter is multiplied by
the side, so a positive value always means the stock has moved, or sits, in the trade's
direction. For a short sell, the close location within a range, between 0 and 1, becomes
1 minus it. Scale-like parameters (ATR, ranges, volumes, traded value) are used as they
are.

**Relative volume** for a span of candles: the stock's volume over those candles, divided
by the mean of the same candles' volume over its previous 20 sessions. At least 10 such
sessions are needed; otherwise it is missing.

**Checkpoints:** the opens of the candles below. Their inputs are only the candles that
have closed.

| | 09:45 calls | Evening picks |
|---|---|---|
| 1 | 10:05 | 09:35 |
| 2 | 10:20 | 09:50 |
| 3 | 10:35 | 10:05 |
| 4 | 11:05 | 10:35 |
| 5 | 12:05 | 11:35 |

**Reported for each parameter:**
- its mean among stopped and other trades, and the standardised difference;
- the stop rate in each fifth of it;
- its single-parameter AUC for the stop.
- **SHAP:** the A1 model's mean absolute contribution (LightGBM's own SHAP values), with
  the direction of its effect.

**A confirmed driver** is one whose stop-rate difference between its top and bottom fifths
has the same sign in development and the holdout, with |t| ≥ 2 in the holdout (two
proportions).

**Hypotheses stated now, to be confirmed or not:**
- **H1, against the market:** trades against the Nifty's direction at the decision are
  stopped more often.
- **H2, volume at the decision:** high relative volume goes with wider moves both ways.
- **H3, the early path:** a trade 0.25 ATR or more against it at the first checkpoint, on
  above-average volume, is stopped more often than average.
- **H4, conviction:** weaker predictions (closer to the gate) are stopped more often.
- **H5, extension:** stocks far from VWAP in the trade's direction are stopped more often.

### Part 2: models trained on them

**Training rows:** a superset of the picks, so the models have enough rows. Each day:
- as buys: the 10 highest predictions above 0;
- as short sells: the 10 lowest below 0.

Each row is traded by the order rule above and labelled from its path.

**Model specification:**
- **Model:** LightGBM, 200 trees, learning rate 0.03, 15 leaves, at least 200 rows a
  leaf, 80% feature and row sampling every iteration, seed 7. Seeds 8, 9 and 10 are
  reported as a robustness check.
- **Refits:** at the start of each calendar quarter from 1 July 2023, on every earlier
  training row (expanding).
- **Separate models** for the two call sets.

**A1, a veto before the entry**, from the decision-time parameters. A pick is not taken
when:
- **V20:** its predicted stop probability is above the 80th percentile of the training
  rows' predictions at the last refit;
- **V33:** the same, above the 67th percentile;
- **VR:** a regression on the trade's return after costs (clipped at ±3%) predicts below
  0.

**A2, an early exit while the trade is open**, from the decision-time and path parameters
at each checkpoint. The trade closes at the checkpoint's open, with slippage, when:
- **E0:** a regression on the rest of the trade (the signed return from the checkpoint's
  open to its exit by the order rule, before costs, clipped at ±3%) predicts below 0;
- **E10:** the same predicts below −0.10%;
- **C50:** a classifier gives the stop, if not yet hit, a probability of 0.5 or more;
- **C60:** the same, 0.6 or more.

A2's training rows are the superset trades still open at each checkpoint.

**AB, both:** the chosen A1 and the chosen A2 together, if each beats F on development.

### Choice and the pass bar

**The choice, on development (July 2023 – 2024), for each call set and each family:**
- **The candidate chosen:** the one with the highest t-statistic of the daily P&L
  difference from F, provided its net return a trade after costs is at least F's.
- **A family with no such candidate** fails on development and is not run on the holdout.

**A pass, on the holdout:**
1. **Better than F:** the daily P&L difference from F has a positive mean with t ≥ 2.0.
2. **Per trade:** its net return a trade after costs is at least F's.
3. **Every year:** the difference is positive in 2025 and in 2026 (to 23 October).

**Also reported:** trades, stop rate, net return a trade, profit factor, ₹ a day, its t,
the maximum drawdown, the worst day and, for A2, the share of trades closed early.

**Multiple tests:** up to six tests are run (two call sets × A1, A2, AB), each against
t ≥ 2.0. One pass among six is weak evidence, which is why a pass changes nothing by
itself (below).

## Study B: investing, the momentum Rankings order

### The trades

**M, the Rankings order:** each month end, the 20 highest by momentum (the percentile of
6- and 12-month returns over volatility) among the 500 most-traded stocks, delisted ones
included (research/momentum_upgrades' inputs). Held in equal weights to the next month
end. Every rupee traded pays 0.195% each way. Results are before tax.

**Periods:**
- **Development:** months ending 2016–2020.
- **Confirmation:** January 2021 – September 2026.
- **A caveat:** both periods have been used for momentum many times; the confirmation is a
  weak test.

**A check first:** M must reproduce 18.4% a year in development and 38.0% in confirmation,
after costs, before tax (research/momentum_upgrades), within 0.5 points. If it doesn't,
the simulator is fixed before anything else runs.

### What counts as an adverse move

**The label:** a pick-month is **adverse** when its return trails the 500's equal-weighted
return for the month by 10 points or more.

Also reported: its drawdown from the entry within the month on daily closes, adverse at
−15% or worse.

### Part 1: which parameters go with adverse moves

**At the month end**, from data up to and including that close:
- momentum and its rank;
- returns over 1, 6 and 12 months;
- volatility over 252 sessions;
- the largest daily return in the last 21 sessions (MAX);
- nearness to the 52-week high (the close over the highest close of 252 sessions);
- distance from the 50-day average;
- volume surge: the median traded value of the last 21 sessions over that of 126;
- delivery: the mean delivery % over 21 sessions, and its change from the mean over 126;
- the share of up days in 126 sessions;
- beta and residual volatility against the 500's equal-weighted daily returns, over 126
  sessions;
- overnight gaps beyond ±5% in 126 sessions;
- size: the log of the median traded value over 126 sessions;
- days since the latest results broadcast, and whether results are due (more than 75
  days);
- the market:
  - the 500's equal-weighted returns over 252 and 21 sessions;
  - its 63-session volatility;
  - the cross-sectional dispersion of 1-month returns.

**After the entry**, at sessions 5 and 10:
- the pick's return and its return against the 500;
- its worst close so far;
- its traded value since entry over its 126-session median;
- its delivery % since entry against its 126-session mean.

**Reported** as in Study A, with the same definition of a confirmed driver.

### Part 2: models and rules

**Training rows:** each month's 60 highest by momentum.

**Model specification:**
- **Model:** LightGBM as in Study A, but at least 100 rows a leaf.
- **Refits:** each January, on every earlier month whose holding month has ended.

**B1, choosing 20 of the top 30** (equal weights):
- **BM:** drops the 10 with the highest predicted probability of an adverse month.
- **BH:** keeps the 20 nearest their 52-week high.
- **BV:** drops the 10 with the highest volume surge.
- **BX:** drops the 10 with the highest MAX.

**B2, selling a holding during the month** (checked at sessions 5 and 10 after the entry;
the sale is at that session's close, and the money is held as cash, at 0%, until the
month end):
- **XR:** sells when the holding is down 8% or more from its entry and its traded value
  since entry is at least 1.5 times its 126-session median.
- **XM3:** a regression on the rest of the month's return against the 500 predicts below
  −3%.
- **XM5:** the same predicts below −5%.

### Choice and the pass bar

**The choice, on development, for each family:**
- **The candidate chosen:** the one with the highest t-statistic of its monthly return
  over M's, after costs, provided its development return a year is at least M's.
- **A family with no such candidate** fails on development.

**A pass, on confirmation**, by either route:
- **The return route:** its monthly excess over M has a positive mean with t ≥ 2.0, and is
  positive in both 2021–2023 and 2024 – September 2026.
- **The risk route:** its return a year is no more than 1 point below M's, and its maximum
  drawdown (monthly) is at least 25% smaller.

## What follows

**Whatever the result, the app gains a recommendation tracker:**
- **At the recommendation:** for every recommendation it makes, it records the live price
  and the day's volume so far (Upstox), with the Nifty's, the moment the recommendation is
  made.
- **Afterwards:**
  - intraday recommendations: price, volume and VWAP every 5 minutes during the session;
  - longer ones: every 30 minutes, and daily.
- **Path measures:** the adverse and favourable excursions, and when they happened, from
  that record.

**The models:**
- **Shadow predictions:** the A1 and A2 models (and B1 and B2 where they apply) are
  retrained on that record as it grows. Their predictions are recorded beside each live
  recommendation.
- **Real orders:** a model that passes here changes nothing by itself. It is offered to
  the owner as a switch only after its forward shadow record agrees with this study's
  result.
- **A model that fails** keeps running in shadow, so its forward record shows whether more
  data changes the result.

## Files

- `PROTOCOL.md`: this; `PROTOCOL.sha256`.
- `paths.py`: Study A's trades, labels, parameters and checkpoints.
- `drivers.py`: Part 1 for both studies.
- `models_a.py`: Study A, Part 2.
- `momentum.py`: Study B.
- `CHOICE.md`: the development choices.
- `dev.txt`, `holdout.txt`: the outputs.
- `README.md`: the write-up.
