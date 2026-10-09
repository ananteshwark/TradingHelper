# What the profitable 3 in 10 intraday traders do

The owner asked what the 3 in 10 intraday traders who make money do differently. This note
combines the published evidence with tests on the app's own data (`research/winners/`).

## Who they are

SEBI studied every individual intraday trader in the cash segment at the ten largest
brokers ([FY23 study](https://www.sebi.gov.in/sebi_data/attachdocs/jul-2024/1721818140715.pdf)).

**Who made money in FY23:**
- **Overall:** 29% made money. Profit-makers averaged ₹5,989 for the year; loss-makers lost
  ₹5,371.
- **Most traders hardly trade:** 57% made fewer than 10 trades in the year. The smallest
  traders had the highest share of losers, 77%.
- **Fewer trades:** profit-makers made 51 trades on average, loss-makers 72. Among traders
  with more than 500 trades a year, 80% lost.
- **Costs:** trading costs took 19% of profit-makers' gains. They added 57% to loss-makers'
  losses, 72% for those with more than 500 trades.
- **Turnover above ₹1 crore** (the serious traders, with over 90% of the turnover):
  - 24% made money, averaging ₹89,172; the 76% who lost averaged ₹74,575.
  - The profitable ones traded less (624 against 779 trades) and in larger sizes
    (₹3.2 lakh a trade against ₹2.5 lakh).
- **Who did better:** older traders (53% losers over 60, 81% under 20), women, married
  traders and Tier-I cities.
- **Experience isn't enough:** after three years of trading, 54% still lost.

**In derivatives:**
- Only 7.2% of individuals made money over FY22–FY24, and 1% made more than ₹1 lakh.
- Algorithms earned 96–97% of the profits of proprietary traders and foreign investors.
- Zerodha's chief executive has said fewer than 1% of active traders beat a fixed deposit
  over three years.

## Much of it is luck

We simulated traders with no skill at all on the app's own data:

- **Trades:** a random liquid Nifty 200 stock on a random 2025 day, a random direction,
  entered at a random time and closed at 15:15.
- **Costs:** Upstox charges and slippage.
- **Sizes:** SEBI's average trade sizes and trade counts.

| Trades in the year (trade size) | Profitable, no skill | With +0.05% a trade | With +0.13% a trade |
|---|---|---|---|
| 5 (₹21,551) | **24%** | 27% | 34% |
| 25 (₹21,551) | 7% | 11% | 19% |
| 75 (₹21,551) | 1% | 2% | 8% |
| 250 (₹2.66 lakh) | 8% | 25% | 72% |
| 742 (₹2.66 lakh) | 1% | 13% | 84% |

- **Small traders look like coin flips:** with a handful of trades and no skill, 24% end
  the year in profit. SEBI's smallest traders were 23% profitable. Most of the "3 in 10"
  are small traders who got lucky with a few trades.
- **Some large traders are skilled:** with hundreds of trades, luck averages out and costs
  win, so only 1% of no-skill traders profit. SEBI found 24% of large traders profitable:
  some of them have a real edge.
- **How long it takes to tell:** a trade's result varies by about 1% either way. An edge of
  +0.13% a trade needs about **250 trades** before it can be told apart from luck (t = 2);
  +0.05% needs about 1,700.

Barber, Lee, Liu & Odean studied all day traders in Taiwan over 15 years
([2014](https://www.sciencedirect.com/science/article/abs/pii/S1386418113000190)):

- About 20% profit in a year, but **fewer than 1% do so predictably**.
- 6.6% won two years running, against 3.9% expected by chance.

In Brazil, 97% of people who day-traded futures for 300 days or more lost money
([Chague et al.](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101)).

## What the skilled minority does

- **They have a track record, and act on it.** Past performance is by far the best
  predictor of next year's (Barber et al.). The top 500 Taiwanese day traders kept earning
  0.38% a day after costs.
- **They specialise.** After past performance, the strongest predictor is trading only a few
  stocks. Their profits are largest in small, volatile stocks and around results
  announcements, where reading information quickly pays.
- **They keep costs down.** They make fewer and larger trades; brokerage is per order, so a
  larger trade costs less as a share of it.
- **They cut losers quickly.** Among Chicago futures traders, the most successful held losing
  positions the shortest (Locke & Mann).
- **They don't chase losses.** Traders who lost in the morning took more risk in the
  afternoon, and those trades lost (Coval & Shumway).
- **Execution matters more than the stock.** In a US prop firm, winners and losers traded
  the same stocks at the same times
  ([Garvey & Murphy](https://www.nuff.ox.ac.uk/Users/MurphyA/Active%20Traders.pdf)). On trades
  against market makers, the winners lost $17 on average and the losers $189.
- **Limit orders alone are not the secret.** The profitable Taiwanese day traders still used
  aggressive orders for two-thirds of their trades. Purely passive day traders did not cover
  their costs.
- **The biggest winners are machines.** Market-making and arbitrage algorithms take nearly
  all the profit in Indian derivatives. A person can't compete with them on speed.

## Tested on the ML intraday calls

The rules below were written down before they were run (`research/winners/PROTOCOL.md`).

| | 2023–2024 (chosen on) | 2025–Oct 2026 (confirmation) |
|---|---|---|
| Market entry at 09:50, exit at 15:15 (the paper rule) | +0.03% a trade, t 0.6, worst trade −18% | **+0.12%**, t 2.2, worst −16% |
| Limit 0.10% better, until 10:25 | +0.01% (77% filled) | not run |
| Limit 0.25% better | −0.01% (67% filled) | not run |
| Stop at half the day's ATR | +0.06%, t 1.3, worst −8% | +0.10%, t 1.9, **worst −4.8%** |

- **Passive limit entries made things worse.** They filled mostly when the price moved
  against the call, so the trades they kept were the worse ones.
- **A stop buys safety, not profit.** It cut the worst trade from −16% to −5% and the
  drawdown from ₹65,000 to ₹56,000, for slightly less return.

**Trade size decides whether an edge survives costs.** Round-trip costs, with 0.04%
slippage, against the ML calls' gross edge of about +0.25% a trade:

| Trade size | Costs | Left of the edge |
|---|---|---|
| ₹10,000 | 0.31% | −0.06% |
| ₹21,551 (SEBI's average) | 0.30% | −0.04% |
| ₹50,000 | 0.17% | +0.09% |
| ₹1 lakh | 0.12% | +0.13% |
| ₹2 lakh | 0.10% | +0.16% |

## What this means here

1. **Trade only with a measured edge, and fewer times.** Most trades happen because there is
   time to fill, not because of an edge.
2. **Judge an edge on about 250 trades, not on a good or bad week.** The ML intraday page
   now shows how far its paper record has got.
3. **Size each trade at ₹1 lakh or more.** Below about ₹50,000, charges eat even a real
   edge.
4. **Enter at the price; don't fish for a better one.** A limit order that fills only when
   the price comes back keeps the losers.
5. **Cap the downside:** a stop, or a fixed daily loss limit, and no extra risk after a
   losing morning.
6. **Specialise and use information.** Results days and a few well-followed stocks are where
   skilled traders earn most. The app holds announcements and results, so that is the next
   study worth doing.
