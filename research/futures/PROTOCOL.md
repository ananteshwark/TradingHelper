# Futures strategies: protocol

Written on 11 October 2026, before any result below was computed. Its SHA-256 is in
`PROTOCOL.sha256`. Choices between variants are made on the development period and
written in `CHOICE.md` before the confirmation period is run, once.

## Question

The futures study (`research/fno`) found that owning momentum winners worked, while
shorting individual stocks, timing and option selling did not. Can futures turn that into
a strategy?

- **S1:** momentum bought through stock futures instead of shares. This compares
  implementations; the long-futures results are partly known from F2.
- **S2 (primary):** hedged momentum. Buy futures on the momentum winners and sell Nifty
  futures against them.
- **S3:** trend following on Nifty and Bank Nifty futures, long or short.

## Data and costs

**Data:**
- NSE's daily F&O bhavcopies and official index closes (Nifty 50, Nifty Bank), January
  2012 – 8 October 2026, as parsed for `research/fno`.
- The survivorship-free equity panel (`research/ml/build_daily.py`).
- Stock futures prices are adjusted for corporate actions with the stock's equity factor,
  as in `research/fno`.

**Costs, today's rates:**
- Round trips, slippage included: 0.119% for a stock futures position, 0.079% for an index
  futures position (`research/fno/PROTOCOL.md`).
- Each month uses the contracts expiring on or after the next rebalance, so every futures
  position pays a round trip each month.
- Shares (for S1's comparison) pay `igs.momentum.cost_pct` on the names bought.

**Cash:** capital not needed as margin earns 6% a year. Returns are reported over cash
(the futures P&L after costs) and in total (plus 6% a year on capital equal to the long
notional). No leverage: notional equals capital.

**Rebalancing:** each month's last session is the signal date. Positions open at the next
session's open, in the contract expiring on or after the next rebalance, and close at the
next rebalance's open.

**Periods:**
- Development: rebalances entered February 2013 – December 2020.
- Confirmation: rebalances entered January 2021 – September 2026, run once. This period has
  been used several times before, so a pass is weaker evidence than a fresh one; a forward
  paper record would be the real test.

## The momentum ranking (S1, S2)

- **Universe:** the stocks with a futures contract trading on the signal date and a 12-1
  month momentum in the equity panel (adjusted close 21 sessions ago over 252 sessions ago).
- **Holdings:** the top ten, equal notional.

## S1: momentum through futures, against shares

- **Futures:** the top ten's futures, plus 6% a year cash.
- **Shares:** the same ten, bought at the open with delivery costs on the names newly
  bought, as the app's momentum portfolio.
- **Reported:** both implementations' monthly returns, Sharpe ratio and drawdowns, and the
  difference. Descriptive: no pass or fail.

## S2: hedged momentum (primary)

- **The positions:** the top ten's futures, and a short position in Nifty futures of h ×
  the long notional.
- **Variants:**
  - (a) h = the portfolio's beta: the average of the ten stocks' betas to the Nifty 50,
    from daily returns over the 252 sessions to the signal date, clipped to 0.5–1.5;
  - (b) h = 1.
- **Return over cash, a month:** the long futures return, less h × the Nifty futures
  return, less costs.

## S3: index trend following

- **Each of Nifty and Bank Nifty futures,** long if the index is in an uptrend at the
  signal date and short otherwise.
- **Variants:**
  - (a) the index's 12-month return (252 sessions) above zero;
  - (b) the index's close above its 200-session average.
- **The portfolio:** half in each index.
- **Return over cash:** the futures P&L after costs. Also reported: both indices held long
  throughout.

## Choosing and passing

- **Choosing (development only):** for S2 and S3, the variant with the higher t-statistic
  of monthly returns over cash, if its mean is above zero; otherwise the strategy fails on
  development.
- **Passing (confirmation):** S2 or S3 passes if its mean monthly return over cash is above
  zero with t ≥ 2.0.

## Also reported

- **Risk:** each strategy's worst month, maximum drawdown, Sharpe ratio, correlation with
  the Nifty, and returns by year.
- **Capital:** the futures contract values on 30 September 2026. Futures need margin of
  roughly 15–30% of notional, and leverage multiplies both returns and drawdowns.
