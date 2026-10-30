# Predictive-model research protocol

Written 9 October 2026, before any model was fitted or any result of this study seen.
Nothing below changes after a holdout result is seen. A candidate is chosen on the
development period alone and written to CHOICE.md before its holdout is computed.

Background: 12 rule-based strategies (6 intraday, 6 swing) failed earlier tests.
Literature: machine-learning combinations of many stock characteristics predict Indian
monthly returns out of sample (Lalwani & Meshram 2022, survivorship-free 1994-2019);
liquidity amplifies momentum in India (Chui et al. 2023); short-term reversal sits in
illiquid stocks and mostly dies after costs; most retail intraday traders lose, costs
being a large part (SEBI, FY2022-23). Delivery STT is 0.1% each side; intraday 0.025% on
the sell side.

## Study A: swing / positional (daily data)

**Data.** NSE equity bhavcopies (EQ series), every trading day from 2 January 2012 to
8 October 2026, so every listed stock is included, delisted ones too (no survivorship
bias). Prices adjusted for splits, bonuses and rights from NSE's own previous close:
factor = PREVCLOSE(t) / CLOSE(t-1) when they differ by more than 1%. Dividends are not
added back. A stock is followed across renames and ISIN changes: a row continues a series
seen in the last 10 trading days with the same symbol or the same ISIN. Delivery from
NSE's MTO files. Nifty 50 from Upstox.

**Universe**, set at each signal date: the 250 stocks with the highest median traded value
over the previous 126 sessions, among those with at least 200 sessions of the last 252 and a
close of at least Rs 20.

**Features**, from data up to the signal date's close only:
- returns over 1, 5, 21, 63, 126 days; 12-1 month (close 21 days ago over 252 days ago);
- volatility of daily returns, 21 and 63 days; the largest daily return in 21 days;
- distance from the 252-day high and low; close over its 50- and 200-day averages;
- sums of overnight (open over previous close) and intraday (close over open) log returns,
  21 days; the last day's gap and close location in its range; the 5-day average location;
- log median traded value, 21 days; traded value 5 days over 63 days; Amihud illiquidity,
  21 days; log close;
- delivery % (21-day mean), its 5-day mean less its 63-day mean, log delivered value;
- beta and residual volatility against the Nifty 50, 63 days; sessions since first seen
  (capped at 756);
- market (same for every stock that day): Nifty 50 returns over 5, 21 and 63 days, its 21-day
  volatility, the share of the universe above its 50-day average, and the cross-sectional
  dispersion of 21-day returns.
Stock features become cross-sectional percentile ranks each day, centred on zero; missing
values sit at the centre. Market features stay raw (standardised on training data for the
linear model).

**Target.** Return from the next session's open to the open H sessions later, less the
equal-weighted universe mean over the same window, then ranked across stocks that day.
H = 5 (weekly) or 21 (monthly). A stock that stops trading inside the window is valued at its
last close.

**Models**, fixed settings, no tuning:
- ridge regression, alpha 1.0;
- LightGBM regression: 400 trees, learning rate 0.03, 15 leaves, at least 1,000 rows a leaf,
  70% of rows (every iteration) and of features per tree, L2 penalty 5, seed 7.

**Walk-forward.** Retrain every January on all rows whose target window ended before that
January (expanding window, from the first signal date in 2013). First model: January 2016.
Development predictions: 2016 to 2020. Holdout predictions: January 2021 to the last
signal whose window closes by 8 October 2026.

**Portfolios.** Monthly (H = 21): signal at the last close of each month, trade at the next
open. Weekly (H = 5): signal at the last close of each week. Hold the top N equally (N = 10 or
20). A holding stays while it ranks in the top 2N; the best-ranked others fill the rest.
Each name bought or sold pays half of the round-trip delivery cost of `igs.momentum.cost_pct`
(charges on Rs 1 lakh plus 0.05% slippage a side, about 0.39% a round trip). A period's
result is the holdings' open-to-open return less the costs of its trades, against the
equal-weighted universe (no costs) and the Nifty 50.

**Candidates:** {ridge, LightGBM} x {weekly, monthly} x {N = 10, 20}: eight. The one chosen
has the highest information ratio of net excess return over the universe in 2016-2020
(monthly series; weekly results summed to months). A tie goes to the simpler: ridge, then
monthly, then N = 20. Baseline reported alongside: the 12-1 momentum top N in the same
framework.

**Pass (holdout, 2021 to October 2026), for the chosen candidate only:**
1. mean monthly net excess return over the universe above zero with t >= 2.0;
2. net excess positive in at least 4 of the 6 calendar years (2026 to date counts);
3. reported, not required: net excess against the momentum baseline, and the net return
   against the Nifty 50.

## Study B: intraday (5-minute data)

**Data.** Upstox 5-minute candles for 197 current Nifty 200 stocks and the Nifty 50,
3 January 2022 to 8 October 2026. A day counts when its first candle is 09:15 and it has at
least 70 candles. (Today's index members: a survivorship bias that favours the strategy.)

**Decision** at 09:45, after the six candles from 09:15 have closed. Enter at the open of
the 09:45 candle, exit at the open of the 15:15 candle. No stop or target. Slippage 0.02%
each side and Upstox intraday charges on Rs 1 lakh (`igs.intraday.costs`), as in the
earlier tests. Stocks averaging at least Rs 50 crore a day over 20 days.

**Features** at 09:45: the gap; return and range (over the 14-day ATR) of the first 30
minutes; its volume over the same 30 minutes' 20-day average; distance from the VWAP; close
location in the 30-minute range; the previous day's return, close location, last-hour return
and open-to-close return; 5- and 21-day returns; ATR as % of price; distance from the 20-day
high and low; log 20-day traded value; 5-day sums of overnight and intraday returns; the
Nifty 50's gap, first-30-minute return, previous-day and 5-day returns; the stock's
first-30-minute return less the Nifty's; the day of the week.

**Target**: the return from entry to exit, clipped at +/-5%.

**Models**: ridge (alpha 1.0, standardised on training data) and LightGBM with the settings
above except at least 500 rows a leaf. **Walk-forward**: retrain every quarter on all earlier
days (expanding), first model January 2023. Development: 2023-2024. Holdout: January 2025
to 8 October 2026.

**Trading rule candidates:** each day, buy up to k stocks with the highest predictions
above zero and sell short up to k with the lowest below zero; k = 3 or 10; either every such
stock or only those whose predicted move is at least 0.15%. {ridge, LightGBM} x {3, 10} x
{no gate, gate}: eight. Chosen: the highest t-statistic of daily net P&L in 2023-2024.

**Pass (holdout), chosen candidate only:** mean daily net P&L above zero with t >= 2.0; a
profit factor of at least 1.1 over its trades; positive in 2025 and in 2026 to date; at
least 100 trades.

## Afterwards

A candidate that passes is offered as forward paper calls first, with its rules frozen, as
with the momentum tracker. One that fails is reported as failed; a new idea after seeing a
holdout needs new data (a forward test), not another look at the same holdout.

## Amendment 1 (2026-10-09T10:27:30Z, before any Study A model was fitted)

NSE's PREVCLOSE in the bhavcopy turned out to be the unadjusted previous close (Reliance's
1:1 bonus on 7 September 2017 shows as a 50% fall), so it can't give adjustment factors.
Study A instead adjusts prices from NSE's daily corporate-action files (Bc*.csv in the PR
archive): bonuses (b/(a+b) for a:b), face-value splits and consolidations (new over old face
value), on the ex-date. Where the text is truncated or its factor is more than 25% from the
ex-date's overnight move, the move snapped to the nearest simple fraction is used; a stated
factor that matches a move within the following three weeks is applied on that day. An
overnight move beyond -40% or +60% with no recorded event (360 ONE's March 2023 bonus and
split is missing from the files) is treated as an unrecorded bonus or split and snapped the
same way. Rights issues, demergers and dividends are not adjusted. Everything else is
unchanged.
