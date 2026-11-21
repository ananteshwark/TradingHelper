# Futures and options: tested

The owner asked whether futures and options could serve the app's strategies. Proprietary
trading firms make their profits mostly in options, and 87.7% of individual traders lost
money in FY26 ([SEBI's study](https://www.businesstoday.in/markets/story/rs91685-cr-lost-88-of-individual-traders-lost-money-in-fy26-options-drove-92-of-losses-550474-2026-08-21)).
This note tests the three uses the app's research pointed to, at today's costs
(`research/fno/`).

## How it was tested

**Data:**
- NSE's daily F&O files, January 2012 to 8 October 2026: every futures and options
  contract traded each day, delisted stocks included. 3,659 days; two are missing from
  NSE's archive.
- NSE's official daily Nifty closing values, which are the final settlement price of
  index options.

**Costs:** at today's rates for every year, including the higher STT since April 2026:
- futures: about 0.12% a round trip for stock futures and 0.08% for Nifty futures, with
  slippage;
- options: 0.15% STT on the premium sold, exchange charges, brokerage, and half an index
  point of slippage a leg.

**Rules:** written down and committed before any result was seen
(`research/fno/PROTOCOL.md`). Choices were made on the early years (`CHOICE.md`), and the
later years were run once.

**Data checks before any result:**
- The official Nifty close matched the Nifty futures' final settlement exactly on all 175
  monthly expiries. A first estimate built from the options' own settle prices was
  inaccurate and was replaced.
- Futures prices tracked their stocks within a median of 0.8 percentage points over each
  holding.

## Results

| Test | 2013–2020 (development) | 2021–2026 (confirmation) | Result |
|---|---|---|---|
| **F1:** sell stock futures for 21 sessions after a results reaction of −5% or worse, with the Nifty bought as a hedge | −0.44% a trade after costs (653 trades) | not run | **failed** |
| **F2:** buy futures on the ten strongest momentum stocks and sell the ten weakest, monthly | +0.94% a month (t 0.8) | +0.44% a month (t 0.6) | **not passed** |
| **F3a:** sell a monthly Nifty iron condor, short strikes 3% or 5% away | −11% / −7% of capital at risk a month | not run | **failed** |
| **F3b:** sell a weekly Nifty iron condor, short strikes 2.5% away, wings 1.5% further | +0.78% a week (t 0.4) | −0.12% a week (t −0.1), 87% of weeks won | **not passed** |

**F1: the "bad results keep falling" effect didn't carry over.** Among the large stocks
that have futures, the shares did not keep lagging the Nifty after a bad reaction:
- before costs, the hedged short lost 0.24% a trade;
- sudden rebounds hurt, such as Syndicate Bank +77% in May 2014 and CG Power +100% in
  early 2019.

The lag found in the results study came from the wider set of 250 most-traded stocks,
many of which have no futures.

**F2: the long side worked, the short side didn't.**

| | 2013–2020 | 2021–2026 |
|---|---|---|
| Strongest ten, bought | +0.80% a month | +1.28% a month (t 1.5) |
| Weakest ten, sold | +0.14% a month | −0.83% a month |
| Long-short, worst month | −33% | −22% |
| Long-short, worst fall | −59% | −31% |

- In a rising market, selling the weakest stocks lost money, and sharp rebounds of beaten
  stocks gave the worst months.
- In these years, momentum's profit came from owning the winners, not from shorting the
  losers, which supports the app's long-only momentum portfolios.

**F3: selling options wins often and loses big.**
- The weekly condor won 87% of weeks in 2021–2026, but its losing weeks ran to −98% of
  the capital at risk, and the average was slightly negative.
- The monthly condors lost money outright in 2013–2020. The Nifty moved more than 3% in a
  month too often for the premium collected: for example, in January 2017 a 5% rally
  through the short call cost 78% of the capital at risk.
- The premium does not cover the occasional big move once costs are paid. Proprietary
  firms profit from options by quoting prices and hedging continuously, not by holding
  condors.

## What this means here

1. **No F&O strategy tested here earns money after today's costs.** None should be traded,
   or even tracked on paper, on this evidence.
2. **Momentum is best used long only,** as the app's momentum portfolios already do.
3. **Selling options is not the safe income it looks like.** A high win rate hides losses
   that, on average, outweigh the wins.
4. **Futures remain useful for hedging,** for example selling Nifty futures to protect a
   portfolio before an event, but that is insurance with a cost, not a source of profit.

## Then: three strategies built on momentum

The tests above showed one thing worked: owning momentum winners. Three futures strategies
built on that were tested next, with rules again committed before any result
(`research/futures/PROTOCOL.md`).

**Setup:**
- 2013–2020 to choose, 2021–September 2026 to confirm, run once. That period has now been
  used several times, so a pass there would be weaker evidence than before.
- Costs as above; capital not needed as margin earns 6% a year; no leverage.

| Strategy | 2013–2020 | 2021–2026 | Result |
|---|---|---|---|
| **Hedged momentum:** buy futures on the ten strongest momentum stocks, sell Nifty futures sized by their beta | +0.20% a month over cash (t 0.5) | **+0.83% a month over cash (t 1.3)**, about 15% a year with cash | **not passed**: positive in both periods, short of t ≥ 2 |
| **Trend following** on Nifty and Bank Nifty futures, long or short | −0.67% (12-month signal) / −0.07% (200-day average) a month | not run | **failed** |
| **Momentum through futures instead of shares** (comparison) | 1.29% a month with cash, against 1.41% for shares | 1.76%, against 1.66% for shares | about the same |

**Hedged momentum is the most promising futures strategy, but not proven.**
- Over 2021–2026, it made about 15% a year including cash. Its worst fall was 18%,
  against 30% for the same stocks unhedged, and it hardly moved with the Nifty
  (correlation 0.07).
- In 2013–2020 it made only +0.20% a month over cash, and it lost in 2016–2018, 2022 and
  2025.
- Selling the Nifty against the winners worked better than selling individual losers
  (F2), but the edge is too small and uneven to count as proven.

**Trend following on two Indian indices lost money.** The 12-month signal was short
during the 2020 rebound, losing 7.3% a month that year. The 200-day signal whipsawed.
Managed-futures funds spread this across dozens of markets; on two correlated Indian
indices it doesn't work.

**Buying momentum through futures instead of shares changed little.** Lower trading costs
were offset by the monthly rollover, the carry built into futures prices and the dividends
a futures holder doesn't receive.

**Capital.** On 30 September 2026:
- a stock futures lot was worth about ₹6.3 lakh at the median (₹4.6–9.3 lakh), so ten
  positions need about ₹63 lakh of exposure;
- a Nifty lot was worth ₹14.8 lakh, and hedging that portfolio takes about five lots.

Margin is roughly 15–30% of exposure, so the strategy can be run with less cash, but then
it is leveraged and its falls grow in proportion.

**What this means:** hedged momentum is the only futures strategy here worth watching.
It is now tracked forward on paper as the fifth portfolio on the Paper tracking page's Momentum tab, "Hedged with
Nifty futures", with the capital it needs ([MOMENTUM.md](MOMENTUM.md)). It should not be
traded with real money on this evidence.
