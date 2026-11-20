# An Aladdin-style engine: protocol

Written on 18 October 2026, before any part of it was run. Its SHA-256 is in
`PROTOCOL.sha256`. The choice is made on the development period and written in
`CHOICE.md` before the confirmation period is run, once.

## Question

The owner asked to replicate BlackRock's Aladdin and use the same kind of engine to make
the app's calls. Aladdin is licensed, hosted software. It is not available to copy or
reverse-engineer, and doing so would breach its licence. What BlackRock has published
about how it is used can be rebuilt and tested:

- **Signals:** its Indian systematic funds combine a composite research score from
  "momentum, value, low volatility, quality, fundamental momentum, and sentiment"
  (JioBlackRock Balanced Advantage Fund, SID of 25 August 2026, as reported). The Flexi
  Cap fund's process is "400+ signals across 1,000+ stocks", with an India-specific risk
  model and each stock's transaction costs.
- **Construction:** Aladdin turns the score into weights by weighing "alpha, risk,
  liquidity, transaction costs and constraints" (sector and stock limits). Universe about
  250 large and mid caps; 50–75 holdings.
- **Risk model:** BlackRock's risk teams describe "linear factor models", "volatility and
  covariance matrix estimation", VaR and stress tests. In its public Scenario Tester,
  exposures come from a security's rank on observable traits.

**The test:** does that construction, a composite score turned into a portfolio by a
factor risk model and an optimizer with costs and limits, make better calls than the
app's Rankings order (momentum, top 20, equal weights)?

## Data

From `research/ml_rating` (point-in-time NSE results and adjusted prices, delisted
companies included):
- the month's 500 most-traded stocks;
- each stock's pillar percentiles (`model.py prepare`): growth, quality, valuation,
  momentum, low volatility, each with higher meaning better;
- `mom_12_1`, `volatility_1y`, `log_mcap`, and the return to the next month end (`fwd_1m`,
  to the last close when a stock stopped trading);
- daily adjusted closes of every NSE stock (`research/promoter/build_all.py`).

Industries come from NSE's current Nifty Total Market and Microcap 250 lists, by ISIN
then symbol. A stock in neither list is "Unclassified": about 43% of the universe in 2015
and 7% in 2025, mostly companies since delisted or merged.

There is no history of news or analyst sentiment, so the sixth signal is left out.
BlackRock's 400+ signals and their weights are not public. This tests the published
concept, not BlackRock's model.

## The engine, each month end

1. **Score:**
   - composite = the mean of a stock's available pillar percentiles (at least 3 of the 5);
   - z = its cross-sectional z-score in the universe, limited to ±3;
   - a stock without a composite gets z = 0.
2. **Expected return:** α = IC × σ × z, with IC = 0.05 (Grinold's rule) and σ the stock's
   monthly risk from step 3.
3. **Risk model** (a linear factor model), from the 252 sessions to the month end:
   - **Exposures** at the month end:
     - one industry factor for each of NSE's industries, plus Unclassified;
     - four styles, size (`log_mcap`), momentum (`mom_12_1`), volatility
       (`volatility_1y`) and value (the valuation percentile), each z-scored in the
       universe, limited to ±3, missing as 0.
   - **Factor returns:** each session, the universe's daily returns regressed on the
     exposures (least squares, no intercept; the industries span the market). A return
     across more than 5 market days is dropped.
   - **Factor covariance:** exponentially weighted with a 63-session half-life, scaled to
     a month (× 21).
   - **Specific variance:** the exponentially weighted mean of each stock's squared
     residuals, same half-life, × 21. With fewer than 63 residuals, its industry's median
     instead.
   - **Covariance:** Σ = X F Xᵀ + D.
4. **Optimizer:** maximize αᵀw − (λ/2)(w − b)ᵀΣ(w − b) − c‖w − w₀‖₁, where:
   - b = the universe weighted by market cap, a Nifty 500 stand-in; stocks without a
     market cap get 0;
   - w₀ = last month's weights after the month's returns, with 0 for stocks that left
     the universe;
   - c = 0.195%, half the 0.39% round-trip delivery cost;
   - λ = 10, 25 or 60 (higher means closer to the benchmark);
   - constraints: w ≥ 0, Σw = 1, each w ≤ 5%, each industry within 10 points of its
     benchmark weight.
   - Solved with cvxpy; a month whose solve fails keeps w₀.

**Variants:** E1 uses the composite score; E2 uses momentum alone (its percentile) as z.
Each is run at the three λ values.

## Compared with

- **M:** the Rankings order. Top 20 by the momentum percentile, equal weights.
- **S:** the composite score's top 20, equal weights. The signal without the engine.
- **The universe:** weighted by market cap (b) and with equal weights.

Every strategy starts in cash and pays c on every rupee traded at each month end,
including the first month and names that leave the universe. A month's return is
Σ w × `fwd_1m` less that cost.

## Periods

- **Development:** months held January 2016 – December 2020.
- **Confirmation:** January 2021 – September 2026.

The momentum percentile was chosen as the Rankings order on these same years
(`research/ml_rating`, `research/ai_tuning`), so M is favoured in both periods.

## Pass rule

**The measure:** the Sharpe ratio of monthly net returns, over cash at 6% a year,
annualized.

1. **Candidate:** the one of the six engine variants with the highest development Sharpe.
2. **It passes** if, in confirmation, its Sharpe is above M's with Memmel's z ≥ 1.65 (the
   Jobson–Korkie test as corrected by Memmel 2003, one-sided 5%).

If it passes, it goes into the app as a paper track first, with its monthly calls and a
forward record, not as orders. If not, nothing is built.

## Also reported (descriptive)

For every strategy, in each period:
- return a year, volatility and worst fall at month ends;
- return over both universes;
- information ratio against b;
- holdings of 0.5% or more, and turnover a year;
- the engine's predicted tracking error against what it turned out to be.

**The calls:** each month, the engine's buys (a holding rising to 0.5% or more from
below) and sells (a holding falling below 0.5%), counted.
