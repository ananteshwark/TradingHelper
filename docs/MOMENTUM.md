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

## The rule

At each month's last close:

- Take the Nifty 200 members (the list the check loads each day; a snapshot is kept when it
  changes) that traded at least ₹50 crore a day over the previous 50 sessions and have a
  year of prices.
- Rank them by the adjusted close 21 sessions ago over the adjusted close 252 sessions ago.
- Hold the best ten equally from the next session's open until the next month's rebalance.
  A stock still in the top ten is kept; one that drops out is sold.

## Two tracks

- **Rule:** the ten best-ranked stocks.
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
- **When there is no review:** the AI-reviewed track holds the rule's picks, and the page
  says why, when:
  - the assistant is off;
  - there is no score run from on or before the signal date, since evidence must not
    postdate the entry;
  - the day's AI budget runs out (the rest are the next-ranked stocks, unreviewed).

## Results

Each holding period runs open to open between rebalances. The current one is marked to the
latest close. A period's result is the average of its holdings' returns, less the round-trip
delivery costs of the names bought at its start, spread over the portfolio. It is shown
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
