# The Plan: momentum stocks, gold and cash, on paper

Paper only: nothing here places an order. The Plan's tab on the **Paper tracking** page and a Telegram
message run, in rupees and whole shares, the mix the research pointed to: 60% in momentum
stocks, 25% in gold and 15% in cash. Before each change the message lists the exact orders
for the next open, for the plan's capital (₹10 lakh unless changed), so they can be placed
by hand.

## Why this mix

Of everything tested, momentum was the one approach that made strong money in every
version, and weighting its stocks towards the calmer ones passed in both halves of the
2013–2026 test ([INSTITUTIONAL_ALGOS.md](INSTITUTIONAL_ALGOS.md)). Its weakness is its
falls: −47% at the worst. Gold hardly moves with momentum stocks (correlation 0.04), and
cash lets the plan buy stocks after a fall without needing courage at the time.

On the same survivorship-free data, February 2013 to September 2026, after trading costs:

| | A year | Worst fall | Worst year | Losing years |
|---|---|---|---|---|
| Nifty 50 (NIFTYBEES) | 11.3% | −36% | −12% | 2 of 14 |
| Momentum alone, risk-weighted | 24.7% | −47% | −30% | 5 of 14 |
| **The Plan, before tax** | **20.1%** | **−30%** | **−18%** | **3 of 14** |
| **The Plan, after tax** | **16.9%** | **−30%** | **−18%** | **5 of 14** |

From ₹10 lakh, the plan would have grown to about ₹82 lakh after tax. By half: 16.3% a year
before tax in 2013–2020 and 25.5% in 2021–2026.

These figures are illustrative, not a pre-registered test: the mix was put together after
the momentum results were known, on data that has been used several times. They leave out
the results and red-flag exclusions below, which history here can't reproduce, and use the
250 most-traded stocks each day in place of the Nifty 200. Edges shrink once widely known;
a realistic aim from here is lower, perhaps 13–16% a year, with falls of 30% along the way.

**Tax** in these figures: short-term gains at 20%, long-term gains at 12.5% above ₹1.25
lakh a year, losses set off within the year but not carried forward; gold at 12.5% after a
year and 30% before. Today's rates are applied to every year.

### A rule that was dropped

The first proposal held 12 stocks and kept a holding until it fell out of the top 25, to
trade and pay tax less. Checked on the same data before building, it did worse, even after
tax, so the plan keeps the tested rule:

| Momentum part | Before tax | After tax |
|---|---|---|
| **Top 10, risk-weighted, monthly, none above 15% (built)** | **20.1%** | **16.9%** |
| Top 10, risk-weighted, monthly, no cap | 20.2% | 17.0% |
| Top 10, kept until rank 15 | 19.8% | 16.7% |
| Top 12, kept until rank 25 (first proposal) | 18.0% | 15.3% |

Holding stocks that have slipped down the ranking gave up more return than the tax and
costs it saved.

## The rules

Set in `config/plan.yaml`:

**Momentum part, 60%.** At each month's last close:
1. The momentum ranking ([MOMENTUM.md](MOMENTUM.md)): Nifty 200 stocks trading at least
   ₹50 crore a day, by their return from 12 months to 1 month ago.
2. Leave out stocks whose results moved them 5% or more below the market in the previous
   21 sessions ([RESULTS_DAYS.md](RESULTS_DAYS.md)), and stocks with a serious red flag
   (a tripped "reject" check, such as heavy promoter pledging or an audit qualification)
   in the latest score run. The next-ranked stock takes the place.
3. Hold the best ten, each weighted by the inverse of its volatility over the last 63
   sessions, none above 15% of the part.
4. Sell a stock that leaves the ten; buy a new one at its target. A stock that stays is
   resized only when it is more than 25% off its target, to avoid small orders.

**Gold, 25%,** through GOLDBEES. **Cash, 15%,** earning 6% a year, as a liquid fund would.

**Rebalancing all three parts** back to 60/25/15:
- at each quarter's first session (January, April, July and October);
- at the next open whenever gold or cash closes more than five points from its share,
  for example after a crash, or after money is added.

## The orders and the paper record

Orders are worked out at the close for the next open, in whole shares: the target amount
divided by the last close, rounded. The Telegram message lists each order with its rough
value and why, and the parts' shares after them. A month with no changes still gets a
message, saying so. Placing the orders spread over the first hour is usually better than
placing them at 9:15.

The paper record then fills them at the next session's open, sells first, after charges:
- stocks: STT 0.1% each side, stamp duty on buys, exchange and SEBI fees, ₹20 brokerage an
  order with GST, the DP charge on sells, and 0.05% slippage each side, about 0.4% for a
  round trip;
- gold: the same without STT, with 0.02% slippage.

It marks the plan to each close. Dividends are credited on their ex-date, and bonuses and
splits change the share counts. A stock that doesn't trade at the open keeps its order
unfilled until the next change.

**Capital.** `capital_inr` in `config/plan.yaml` (₹10 lakh) is read once, when the plan
starts. To add money every month, or withdraw some, use **Add or withdraw money** on the
page (administrators). It is applied at the next open and buys or sells units at the last
close, so it never counts as a return. Added money lifts cash above its share, so the next
orders invest it across the three parts.

**Returns** are per unit: the value of ₹100 at the start, after charges. The page compares
it with the Nifty 50 over the same days.

The plan starts on the first daily job after deploying: it records the capital at the
latest close and sends the first orders for the next open. The daily job's step **the Plan
(paper)** runs after the results reactions, so the month's exclusions are known. It loads
about 14 months of prices once a run.

Tables: `plan_day` (the plan after each session), `plan_rebalance` (the orders and their
fills), `plan_flow` (money added or withdrawn).

## The habits that matter more than the picks

- **Don't sell the momentum part in a crash.** A fall of 30% will come; every
  market-timing rule tested here lost money.
- **No option selling, no shorting single stocks, no borrowing to invest:** every test of
  these lost ([FUTURES_OPTIONS.md](FUTURES_OPTIONS.md)).
- **Add money regularly and review twice a year,** not daily.
- **Only invest money you won't need for five years or more,** with an emergency fund kept
  separately.

This is a record of a rule, not personal investment advice.
