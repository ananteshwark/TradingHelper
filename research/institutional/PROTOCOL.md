# Institutional portfolio logic on the app's data: protocol

Written on 11 October 2026, before any result below was computed. Its SHA-256 is in
`PROTOCOL.sha256`. The choices between variants are made on the development period only
and written in `CHOICE.md` before the confirmation period is run, once.

## Question

Large systematic managers rarely rely on one signal held at a fixed size. They size
positions by risk, combine factors, switch off in falling markets, and spread bets
widely (`docs/INSTITUTIONAL_ALGOS.md`). Do these practices improve the app's momentum
portfolio (12-1 month momentum, top ten, monthly) on Indian data, after costs?

Known before this test: on 197 current Nifty 200 stocks, the momentum rule lagged the
equal-weighted basket in 2013–2020 and beat it in 2021–2026 (`docs/MOMENTUM.md`). Nothing
is known about the variants below on this data.

## Data

- The survivorship-free daily panel built from NSE bhavcopies for the ML study
  (`research/ml/build_daily.py`): on each session, the 250 most-traded stocks; prices
  adjusted for splits, bonuses and rights.
- The market: NIFTYBEES (the Nifty 50 ETF), adjusted closes, from the same files.
- Cash earns 6% a year (a liquid fund), accrued daily.
- 5-minute candles from Upstox for 197 Nifty 200 stocks, February 2022 – October 2026, for
  the execution study only.

## The base portfolio (M)

- **Signal:** the last session of each month, at the close. Universe: the panel's members
  that session with 12-1 month momentum (close 21 sessions ago over close 252 sessions ago)
  and at least 200 daily returns over the past 252 sessions.
- **Holdings:** the ten highest momentum, equal weight, bought at the next session's open
  and held to the next month's first-session open.
- **Valuation:** daily, at adjusted closes; a stock with no price that day is valued at its
  last close. A stock that stops trading is valued at its last close until the next
  rebalance.
- **Costs:** at each rebalance, every purchase (new names and top-ups, as a share of the
  portfolio) pays `igs.momentum.cost_pct` at its raw open price: the round trip of delivery
  charges on ₹1 lakh plus 0.05% slippage a side, about 0.39%. Sales pay nothing more.

## Hypotheses

Each variant is compared with M over the same days, with the same costs.

- **H1, volatility targeting** (Barroso & Santa-Clara 2015; standard risk sizing): at each
  signal, the share in stocks is min(1, target / σ), where σ is the annualised volatility of
  M's daily returns over the previous L sessions and the target is the median of σ at the
  previous signals (at least six; until then the share is 1). The rest is cash. No leverage:
  delivery can't borrow. Variants: L = 63 or 126 sessions.
- **H2, momentum combined with low volatility** (factor combination, as in AQR's and NSE's
  multi-factor indices). Volatility: the standard deviation of daily returns over the past
  252 sessions. Variants: (a) the ten best by the average of the momentum percentile and
  the low-volatility percentile; (b) the ten best by momentum among stocks with
  volatility below that session's median. Also reported: the ten lowest-volatility stocks
  alone (LV).
- **H3, market trend filter** (time-series momentum; CTA and Faber-style). At each signal,
  hold M if the market is in an uptrend, else cash. Variants: (a) NIFTYBEES close above its
  200-session simple average; (b) NIFTYBEES 12-month return (252 sessions) above zero.
- **H4, risk-weighted holdings** (inverse-volatility weights, as in risk budgeting): M's ten
  names, weighted by 1 / their 63-session volatility. No variants.
- **H5, breadth** (Grinold's fundamental law: more, smaller bets): the top 30 instead of
  the top 10. No variants.

## Periods

- **Development:** rebalances entered in January 2013 – December 2020.
- **Confirmation:** rebalances entered in January 2021 – September 2026, the last one
  exiting at the 1 October 2026 open. Run once, after `CHOICE.md`.

## Measures

From daily net returns: annualised return (CAGR), volatility, Sharpe ratio (return over
cash, annualised with √250), maximum drawdown, and the share of time invested.

Bootstrap: a paired circular block bootstrap of the daily returns (63-session blocks,
2,000 draws, seed 7) gives the share of draws in which the variant's Sharpe ratio exceeds
the comparison's.

## Choosing (development only)

For H1, H2 and H3, the variant that meets its criterion below on the development period;
if both do, the one with the higher Sharpe ratio; if neither does, the hypothesis stops
and is reported as failed on development. H4 and H5 have one form each and go to
confirmation regardless.

## Passing (confirmation)

- **H1:** Sharpe above M's with a bootstrap share of at least 0.8, and a smaller maximum
  drawdown.
- **H2:** Sharpe above M's and above LV's, each with a bootstrap share of at least 0.8.
- **H3:** a smaller maximum drawdown than M's, and a Sharpe ratio at least M's.
- **H4, H5:** Sharpe above M's with a bootstrap share of at least 0.8.

Five hypotheses are tested; a pass at these bars is evidence, not proof. Anything that
passes is a candidate for a paper track in the app, not for real money.

## Execution study (descriptive, no pass or fail)

Institutions trade rebalances with execution algorithms (VWAP, TWAP) rather than at a
single moment. For M's rebalance trades in stocks with 5-minute data (entries from March
2022), compare executing at the 09:15 open with: the first hour's VWAP (09:15–10:15), the
full session's VWAP, the session's TWAP (average of 5-minute closes), and the closing price
(VWAP of 15:00–15:30, as NSE computes it). Report, in % of the open price, the mean and
median saving (for buys, the open minus the price; for sells, the price minus the open),
and the spread of execution prices around the session VWAP, for rebalance trades and for
all stock-days.
