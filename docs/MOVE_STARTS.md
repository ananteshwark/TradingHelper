# What happens just before big moves start, tested

The owner's idea: find the events around the dates when stocks began big rises or falls,
and use the same events to call stocks early in their moves.

As before, the rules were written down and fingerprinted before any result
(`research/move_starts/PROTOCOL.md`, with the event definitions in `EVENTS.md`). Choices
were made on 2013–2020, and 2021–2026 was run once.

## How it was tested

**Big moves**, found in hindsight:
- **A rise:** a stock beat the market by 30% or more over the next three months (63
  sessions), starting from its lowest close of the surrounding month.
- **A fall:** it lagged the market by 25% or more, starting from its highest close.
- In 2013–2020 there were 2,373 rises and 2,758 falls among about 800,000 investable
  stock-days (stocks trading at least ₹2 crore a day).

**Events**, each dated to when the market could know it:

| Kind | Events |
|---|---|
| Price and volume | a day 5% above or below the market, a 3% gap up or down, traded value or delivery 3× normal, a 52-week high |
| Company news | every NSE announcement since 2013 (1.45 million; about 600,000 not routine filings), grouped into order wins, acquisitions, rating upgrades and downgrades, fund raising, buybacks, bonuses and splits, senior resignations, pledges, defaults, exchange queries about price moves, investor meets and press releases |
| Ownership and flows | promoter purchases and sales, block deals by institutions, Nifty index inclusions, futures open-interest build-ups |
| Results | results board meetings |

Bulk deals had to be left out: NSE's listing returns at most 70 a request, even for a single
day.

## What comes before big moves (2013–2020)

The **lift** compares how common an event is in the five sessions before a move starts
with how common it is on any day. A lift of 3 means three times as common.

**Before big rises:**
- Before a big rise, the stock had usually just fallen hard:
  - a day 5% below the market was 3.2× as common;
  - a 3% gap down was 2.0× as common.
- Company news was not more common before rises than on any other day:
  - order wins 0.7×, acquisitions 1.0×, investor meets 1.0×, buybacks 0.7×, bonuses
    0.6×;
  - promoter buying 1.0×.
- Once the rise had begun, in its first five sessions, +5% days and gaps up were three
  times as common. By then the start has passed.

**Before big falls,** the stock had usually just spiked:
- a day 5% above the market 3.5×, a gap up 2.6×;
- traded value 3× normal 2.2×, delivery 3× normal 2.0×;
- a 52-week high 2.1×, the exchange asking about the price move 1.6×.

Rating downgrades were 7× as common, but rare: 32 in eight years.

So the turning points are real and have a pattern, but it is the opposite of what one
might expect:
- big rises start after sharp falls;
- big falls start after sharp rises on heavy volume.

## Can that be traded?

| Rule | 2013–2020 | 2021–2026 | Result |
|---|---|---|---|
| Buy after a −5% day (where rises start), hold a month | −1.55% a month (t −6.1) | not run | **loses** |
| Buy after a promoter purchase, hold 3 months (best buy rule) | +1.11% a month (t 1.4) | +0.45% a month (t 0.5) | **failed** |
| Sell futures after a +5% day, hold a month (best sell rule) | +0.55% a trade, +0.69% a month (t 2.75) | +0.51% a trade, +0.43% a month (t 1.2) | **not passed** |
| A combined score from all events (logistic regression) | buy: −1.09% a month on 2019–2020; sell: +16.7% a month but only 31 trades | not chosen | — |

**Why buying the early signs of a rise doesn't work:**
- Rises do start after sharp falls, but most sharp falls keep falling. Buying after every
  −5% day lost 1.4% a trade over the next month.
- The falls that turn into rallies can't be told apart from the rest at the time. The
  "start" can only be marked in hindsight.
- No company announcement, flow or price event, alone or combined, picked rises early
  enough to beat the market after costs.

**Selling after spikes came closest:**
- Stocks with futures that jumped 5% above the market gave back about 0.5% over the next
  month, after futures costs, in both periods.
- That's too small and uneven to trade: 2025 lost 1.6%.

## What this means

1. **No early-mover call list is built.** Nothing passed, and calls from these events
   would have lost money or done no better than chance.
2. **Big moves can't be called at their start from public information.** The early signs
   only look clear afterwards. This matches every earlier test here: what worked was
   holding winners (momentum), not catching turns.
3. **Spikes are better treated as a warning than a buy signal.** Big falls start after
   sharp rises on heavy volume, and stocks with futures gave back about half a percent the
   following month. It is consistent with the intraday rules, which stopped calling stocks
   already 3% or more from the previous close ([INTRADAY.md](INTRADAY.md)).
4. **Company announcements didn't help on their own.** Order wins, acquisitions,
   investor meets and buybacks were no more common before big rises than on ordinary days.
