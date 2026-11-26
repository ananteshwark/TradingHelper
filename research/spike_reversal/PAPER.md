# Spike reversal, forward on paper: the price-band rule

Written on 28 October 2026, before the paper track recorded anything. Its SHA-256 is in
`PAPER.sha256`. The track is `igs.intraday.spikes`, on the Paper tracking page's Spike
reversal tab.

## Why

The test (`README.md`) didn't pass. Its worst trades ended locked at a price band: 168 of
1,431, losing 3.7% each, 132 of them short sales of stocks still rising. Those trades
might not even have closed.

The owner asked for the idea to be tested forward on paper: leave out the trades whose band
is close. Both test periods are used, so only data from now on can test it. Nothing in
this rule was fitted to the past data: it wasn't run on it, and NSE's past band lists
aren't here.

## What is recorded

Every trading day, from the first day the job runs after this is deployed.

- **Stocks:** NSE's EQ series, with a median traded value of at least Rs 2 crore over the
  previous 20 sessions (the loaded bhavcopies, `price_eod`), still trading at the last one.
- **Live data, every 5 minutes** (the intraday job):
  - Upstox's day high and low for every such stock (market quotes, 500 a request);
  - for those whose high is at least 12% above their low, today's 5-minute candles from
    Upstox. Only candles that have closed are used.
- **The rule** is the test's chosen variant (`CHOICE.md`), unchanged:
  - **spike:** a 5-minute close 12% above the lowest low of the candles from 25 minutes
    before it, or 12% below the highest high, from the 09:15 candle to the 14:55 one; the
    first of the day counts;
  - **flat candle:** the first at or after the spike, starting by 14:55, with
    |close − open| ≤ 1% of the open and a high above its low;
  - **entry:** the next candle's open, against the move: a short sale after a rise, a buy
    after a fall;
  - **exit:** a 4% target as a limit order, else the 15:15 candle's open. No stop.
- **Costs:** Upstox intraday charges on Rs 1 lakh a trade (`igs.intraday.costs`), 0.15%
  slippage a side on the entry and the 15:15 exit, none on the target.

## Two records

- **A, every spike:** the test's rule as it was.
- **B, the band rule:** A's trades except those whose band on the losing side is close.
  - The band is from NSE's security list for the day (`igs.intraday.price_bands`) and the
    previous close.
  - A **short sale** is left out when the stock has a fixed band and the upper band is
    less than 4% above the entry: no more room against it than the target has for it.
  - A **buy** is left out when the stock has a fixed band and the lower band is less than
    4% below the entry.
  - Stocks with no fixed band (those with stock futures, whose bands are dynamic) are
    always kept.
  - A trade whose band can't be found (the list unavailable, or no previous close) is left
    out of B and counted.

## How it will be judged

- **The measure:** each record's daily net P&L in rupees over every session the job ran:
  the day's trades added up, Rs 1 lakh each, and zero on a day without one.
- **Too early** until B has at least 100 trades.
- **B passes** with a mean above zero and a t-statistic of at least 2.0.
- **Also reported, not tests:**
  - A beside B, so the band rule's effect shows;
  - how many trades B left out, and how those trades did;
  - trades whose exit candle has no price range (a lock).

A pass makes the rule a candidate for real orders, which needs the owner's decision and
its own change. Nothing here places an order.

## Telegram

- **Spike alerts:** every liquid stock that moves 12% or more within 30 minutes, once a
  stock a day, at any time in the session, to the spikes chat. They are information, not
  calls. The threshold and the liquidity floor are set in `config/alerts.yaml`
  (`intraday_spikes`). The paper track keeps 12% and Rs 2 crore whatever they are set to.
- **The paper track** sends each entry, with what the band rule does with it, and after
  15:20 the day's results, to the paper chat.

## Known differences from the test

- Upstox's live candles can differ slightly from its historical ones.
- The job runs every 5 minutes, so a decision is recorded up to 5 minutes after the candle
  that settles it. Prices are those candles' opens, as in the test.
- On a busy day at most 60 stocks' candles are fetched a scan, those with paper trades
  first and then the longest unchecked, so a spike can be found a scan late. A spike first
  found after its entry candle has passed is still recorded, at that candle's open.
