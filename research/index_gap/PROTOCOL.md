# Protocol: rebuilding the Nifty200 Momentum 30 in the engine, and the gap to the backtest

Written before any rebuilt or decomposed figure is computed.

## The question

The Rankings order's backtest (`research/momentum_upgrades`, strategy M: the 500
most-traded stocks, the 20 with the best momentum, equal weights, monthly) made 38.0% a year
after costs in January 2021 – September 2026. The Nifty200 Momentum 30 price index made
14.4% a year over the same months (NSE's month-end closes, `ind_close_all`). The 9.3% quoted
before was a different window: the five years from September 2021, and probably the total
return index.

So the gap is 24 points a year. Two questions:
1. Does the engine reproduce the index from its own data and NSE's published rules? If not,
   the engine is suspect.
2. Starting from the index, which of M's differences account for the gap, and how much of
   it each one accounts for?

## Data

- **Prices.** `research/promoter/build_all.py`'s panel of every NSE EQ/BE stock, February 2012
  to 8 October 2026: closes adjusted for bonuses and splits, not dividends; delisted stocks
  included; traded value per session. It is the panel M was run on.
- **The index.** NSE's closing values (`ind_close_all_DDMMYYYY.csv`, nsearchives) at each month
  end: the Nifty200 Momentum 30, Nifty 200 and Nifty 500 price indices. These are price
  indices, comparable with the panel's closes.
- **Shares, for market caps.** NSE's market capitalisation file of 30 October 2026 (the mcap
  file in `PR301026.zip`): each security's issue size.
  - A company's market cap on an earlier date is that count times its adjusted close then.
    This assumes no shares were issued or bought back other than by bonuses and splits,
    which the adjusted closes already undo.
  - A stock not in that file (delisted before October 2026) has no market cap and can't be
    in the rebuilt index's universe. How many such stocks would have been among the 200
    largest by traded value is reported, as a measure of that survivorship.

## The period

Monthly returns from 31 December 2020 to 30 September 2026, the months of M's confirmation
period. Each figure is a compound annual return over those 69 months, before costs unless
stated.

## A. The index as published

The Nifty200 Momentum 30 price index's month-end closes.

## B0. The index rebuilt with NSE's published rules

Approximations are marked †.

**Universe:** the 200 companies with the largest average full market cap over the last 126
sessions at the cut-off, among EQ/BE stocks with at least 252 sessions.
- † NSE uses its actual Nifty 200 list, chosen by free-float market cap with liquidity
  screens. That list's history isn't available here, and free float isn't known.

**Score.** At the cut-off:
1. Take each stock's 6- and 12-month price returns (126 and 252 sessions, close to close).
2. Divide each by the annualised volatility of daily log returns over 252 sessions.
3. Turn each ratio into a z-score across the universe, winsorised at ±3.
4. Combine them 50/50: Z.
5. The normalised score is 1 + Z when Z ≥ 0, and 1 / (1 − Z) otherwise.

**Selection:** 30 stocks.
- The best 15 by score are always in.
- Existing constituents ranked up to 45 stay, best ranks first.
- The remaining places go to the best-ranked others.
- † NSE's exact buffer isn't published in the material at hand. So B0 is also run with no
  buffer (the best 30), and both are shown.

**Weights:** full market cap × normalised score.
- Capped at the lower of 5% and 5 × the stock's weight in the universe by full market cap.
  Excess is redistributed pro rata, and repeated until no weight exceeds its cap.
- † Full market cap, not free float.
- Weights drift with prices between rebalances.

**Timing:** the cut-off is the last session of May and of November. The new portfolio is held
from the close of the last session of June and of December.

**How well it reproduces A:**
- the annual return of B0 against A;
- the correlation of their monthly returns;
- the annualised tracking error;
- the same for the no-buffer version.

## The steps from B0 to M

Each step changes one thing, on top of the steps before it, in this order:

| Step | What changes |
|---|---|
| B1 | Equal weights instead of market cap × score |
| B2 | 20 stocks instead of 30, no buffer |
| B3 | Monthly: the signal at each month end, held from that close (M's timing), instead of semi-annually with a month's lag |
| B4 | M's score: the average of the 6- and 12-month return-over-volatility percentiles (`p_momentum`), instead of NSE's normalised z-score |
| B5 | M's universe: the 500 most-traded stocks by 126-session median traded value, instead of the 200 largest. B5 is M before costs. |
| B6 | M's costs, 0.195% of each rupee traded each way. B6 is M, and should give 38.0%. |

**What is reported for each step:** its annual return, and the change from the step before.
That change is the step's share of the gap.

**The order matters,** since effects interact. As a check, the steps are also run with the
universe change (B5) first, and both attributions are reported.

## Checks on M's data

Before reading the attribution, the 15 largest single-stock monthly contributions to B5's
return are listed and checked against NSE's unadjusted bhavcopy closes and corporate actions.
A move that comes from a missing or wrong adjustment, a symbol mix-up, or a relisting would be
a data error in the engine, and would be reported as such.

## What follows

This is a decomposition, with no pass or fail.
- **If B0 doesn't reproduce A within a few points a year** (beyond what the † approximations
  explain), the engine's data is the first suspect, and the cause is found before anything
  else is concluded.
- **Otherwise** the report says how much of M's 38% comes from each difference, and which of
  those a real portfolio could capture (the universe's liquidity, trading at the signal's own
  close, costs at size).
- **Nothing in the app changes from this study.** A data error found in the engine would be
  fixed separately.
