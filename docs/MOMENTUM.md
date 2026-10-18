# Momentum paper portfolios

Paper only: nothing here places an order. The page **Momentum (paper)** and a Telegram
note each month track, forward, the one swing rule that came close in a 14-year backtest,
with and without an AI review of its picks.

## Why this rule

On 9 October 2026, six long-only swing strategies were tested on Upstox's daily candles
(adjusted for bonuses and splits) for 197 Nifty 200 stocks, 2013 to October 2026. The
strategies and pass rules were fixed before any result was seen:

- Signals use the close and trades enter at the next day's open.
- Costs are delivery charges on ₹1 lakh plus 0.05% slippage each side, about 0.39% a round
  trip.
- Each trade is measured against the equal-weighted average of the same stocks over the
  same days, which removes the market's rise and most of the bias from using today's index
  members.

| Strategy | 2013–2020 vs basket, per trade | 2021–2026 vs basket, per trade |
|---|---|---|
| 12-1 month momentum, top 10 monthly | −1.5% (t −1.3) | **+5.7% (t 1.9)**, positive in 4 of 6 years |
| 55-day breakout trend | −1.3% | −0.7% |
| 52-week-high breakout | −1.1% | −0.1% |
| Pullback in uptrend (2-day RSI) | −0.3% | −0.4% |
| Weekly reversal | −0.4% | −0.3% |
| Results-style gap drift | −1.4% | −0.7% |

None passed. Momentum beat the basket strongly from 2021 (+12% in 2021, +21% in 2023 per
trade) but lagged it from 2013 to 2020, so it may work only in some market phases. An AI
strategy can't be backtested honestly on past data, because a model may know from its
training what happened next. A forward record is the honest test of both.

### Intraday strategies tested first

Six intraday strategies were tested before the swing ones, on Upstox's 5-minute candles
for the same stocks. They were fixed before any result was seen:

- Each trade enters at the open of the candle after its signal and closes by 15:15.
- A candle that touches both the stop and the target counts as the stop.
- Costs are Upstox intraday charges on ₹1 lakh plus 0.02% slippage each side, about 0.12% a
  round trip.

The first test (July–October 2026) found nothing. The same rules, unchanged, were then run
on February 2022 to October 2026: 154,065 trades, with 2022–2024 as the test and 2025–2026
as the confirmation. Passing needed a mean of more than +0.05R after costs over 2022–2024,
then a positive mean (t ≥ 1.5, profit factor ≥ 1.1) over 2025–2026.

| Strategy | Trades | Average move before costs | R after costs, 2022–2024 | R after costs, 2025–2026 |
|---|---|---|---|---|
| Opening-range breakout (30 min) | 39,735 | +0.03% | −0.11 | −0.14 |
| Momentum into the close | 36,948 | +0.04% | −0.17 | −0.18 |
| Previous day's high/low breakout | 34,161 | −0.01% | −0.14 | −0.17 |
| VWAP pullback | 29,268 | +0.02% | −0.19 | −0.18 |
| Midday reversal | 10,433 | −0.11% | −0.08 | −0.05 |
| Gap fade | 3,520 | −0.01% | −0.14 | −0.17 |

None passed. Each one lost money after costs in every year from 2022 to 2026. The best
average move before costs, +0.04%, is a third of the round-trip cost. On these stocks,
5-minute signals of this kind don't move far enough to pay for the trade.

## The rule

At each month's last close:

- Take the Nifty 200 members (the list the check loads each day; a snapshot is kept when it
  changes) that traded at least ₹50 crore a day over the previous 50 sessions and have a
  year of prices.
- Rank them by the adjusted close 21 sessions ago over the adjusted close 252 sessions ago.
- Hold the best ten equally from the next session's open until the next month's rebalance.
  A stock still in the top ten is kept; one that drops out is sold.

## Seven tracks

- **Rule:** the ten best-ranked stocks.
- **Rankings order:** the ten of the same liquid Nifty 200 stocks that the Rankings page
  puts first in the signal date's score run, Rejected stocks left out.
  - **What that order is:** since 17 Oct 2026, momentum: the 6- and 12-month return
    divided by volatility, ranked against every stock in the universe, with red flags and
    the capped AI and sentiment overlays. It differs from the rule's 12-1 return. The other
    pillars are warnings, not score ([research/ml_rating](../research/ml_rating/README.md)).
  - **Why a track:** that order was chosen after a backtest had been seen, so this is its
    forward record, next to the rule and the basket.
  - **Its history** ([research/ai_tuning](../research/ai_tuning/README.md), shown on the
    page). From 2016 to September 2026, among the 500 most-traded stocks, after costs:
    - the top 20 made 28% a year (about 22% after tax), with a worst fall of 39%;
    - the top 10 made 27% a year, with a worst fall of 46%;
    - the Nifty 50 made 10% a year.
    The same data chose momentum, so these figures flatter it. Letting the past set the
    weights instead did worse in all four versions tried.
  - **When the run scored fewer than ten of them:** the rule's next stocks fill the places,
    and the note names them. With no score run from on or before the signal date it holds
    the rule's picks and says so.
  - **Start:** it started at the first month start after it was deployed.
- **Hedged with Nifty futures:** the ten best-ranked stocks that have futures, bought as
  futures, with Nifty futures sold against them.
  - **Which stocks have futures:** NSE's F&O lot list (`fo_mktlots.csv`, source
    `nse_fo_lots`), loaded by the check each day; a snapshot is kept when it changes.
    Stocks the list doesn't have are skipped, and the next-ranked take their place.
  - **The hedge:** the Nifty is sold at the ten's average beta to the Nifty 50 (daily
    returns over 252 sessions, kept between 0.5 and 1.5). So the track earns how much its
    stocks beat the market, not the market's own moves.
  - **Measurement:** on the stocks' and the Nifty's open-to-open moves. Futures follow
    these less their carry, which the hedge cancels.
  - **Costs:** each month is a futures round trip on every position (0.119% a stock, 0.079%
    for the Nifty), and capital not needed as margin earns 6% a year.
  - **Capital:** the rebalance note gives what one lot of each stock and the hedging Nifty
    lots would take; margin is roughly 15–30% of that.
  - **Why:** it was the most promising futures strategy tested, positive in both 2013–2020
    and 2021–2026 but short of the bar set ([FUTURES_OPTIONS.md](FUTURES_OPTIONS.md)).
  - **Start:** it started at the first month start after it was deployed.
- **Rule, risk-weighted:** the same ten, each weighted by the inverse of its volatility
  (the standard deviation of daily returns over the previous 63 sessions), so calmer
  stocks get more. A stock with too little history gets the median stock's weight.
  - In the test of large managers' practices
    ([INSTITUTIONAL_ALGOS.md](INSTITUTIONAL_ALGOS.md)), it beat equal weights in both
    halves: Sharpe 0.61 against 0.53 in 2013–2020 and 0.87 against 0.71 in 2021–2026.
  - Costs are charged on each stock bought, at its weight.
  - It started at the first month start after it was deployed.
- **Rule, skipping bad results:** the ten best-ranked stocks without those flagged by a
  results reaction of −5% or worse against the market in the 21 sessions before the signal
  date, as recorded by then ([RESULTS_DAYS.md](RESULTS_DAYS.md)). The next-ranked unflagged
  stock takes a flagged one's place, and the rebalance note names the stocks skipped. It
  started at the first month start after it was deployed: earlier months would have had
  no flags recorded in time.
- **AI-reviewed:** the ten best-ranked stocks the AI keeps.
  - The AI (assistant feature `momentum`) reads what the app holds on each candidate in
    rank order, as of the signal date's score run: results, filings, shareholding and
    pledges, insider trades, news, brokers' calls, red flags and the Screener.in export.
    It reviews at most 20 a month.
  - It answers keep or avoid. It avoids only for a specific, documented reason it cites,
    such as regulatory or forensic action, accounting red flags, heavy promoter pledging
    or selling, sharply worse results, or a corporate event.
  - It is told not to avoid a stock for its valuation or past rise, and to ignore anything
    after the data's date.
  - The next-ranked stock it keeps takes the place of one it avoids.
- **Aladdin-style engine:** every stock ranked, weighted by a portfolio engine of the kind
  BlackRock describes for Aladdin (`igs.risk_engine`,
  [research/aladdin_engine](../research/aladdin_engine/README.md)).
  - **The score:** the Rankings order's momentum (6- and 12-month return over volatility),
    as a z-score. Expected return is 0.05 × the stock's risk × that z-score.
  - **The risk model:** industry factors (NSE's sectors) and four styles (size, 12-1
    momentum, volatility, value from the latest score run). Their covariance and each
    stock's own risk come from a year of daily returns.
  - **The weights:** maximize expected return less a risk penalty (λ 10) against the
    stocks' market-cap weights, less trading costs.
    - Constraints: long only, at most 5% a stock, each industry within 10 points of its
      market-cap weight.
    - Weights under 0.25% are dropped. Usually 30–60 stocks.
  - **Costs:** paid on every weight it adds, kept stocks topped up included, not only on
    new names. The note says how much it bought and its predicted tracking error.
  - **Why it is followed:**
    - in its 2016–2026 test it was smoother than the Rankings order but made less:
      24.4% a year against 37.0% in 2021–2026, Sharpe 0.95 against 1.16;
    - it did not pass, and the owner asked to follow it on paper anyway.
  - **Start:** it started at the first month start after it was deployed.
- **When there is no review:** the AI-reviewed track holds the rule's picks, and the page
  says why, when:
  - the assistant is off;
  - there is no score run from on or before the signal date, since evidence must not
    postdate the entry;
  - the day's AI budget runs out (the rest are the next-ranked stocks, unreviewed).

## Results

Each holding period runs open to open between rebalances. The current one is marked to the
latest close. A period's result is the average of its holdings' returns (weighted, for the
risk-weighted track and the engine), less the round-trip delivery costs of what was bought
at its start, spread over the portfolio. It is shown
next to:

- the equal-weighted average of the stocks ranked (the basket, no costs);
- the Nifty 50, from the signal date's close.

The totals compound the periods since the start.

The first portfolios were formed on the first daily job after this was deployed, from the
latest month end then (30 September 2026, entered at the 1 October open); later ones at
each month start. The daily job's step **momentum paper portfolios** does the work. It loads
about 14 months of prices once a run, and sends the Telegram note when it rebalances.

Tables: `index_member` (index snapshots), `momentum_rebalance` (each month's holdings and
the AI's reviews), and `momentum_period` (each period's results).
