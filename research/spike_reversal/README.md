# Spike reversal: fade a 12% move in 30 minutes

The owner's rule: "If any stock rises or falls more than 12% in 30 minutes, it is going to
recover by 4%, so buy or sell at the moment the candle becomes horizontal, with a target
profit of 4%."

The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed (a0d51c1) before
any trade was simulated. The choice (`CHOICE.md`) was committed (8b617e1) before the
confirmation period was run.

**Verdict: it doesn't pass.**
- It loses a little in 2022–2024 and makes a little in 2025–2026, never with a
  t-statistic near significance.
- Across both periods it made ₹30 a trade on ₹1 lakh, about 0.03%.
- Nothing is built into the app.

## What was tested

- **Stocks:** every NSE stock in the EQ series with a median traded value of at least
  ₹2 crore over the previous 20 sessions, January 2022 – 8 October 2026. The Nifty 200
  alone has too few spikes: 89 stock-days in almost five years.
- **The spike:** a 5-minute close 12% above the lowest low of the last 30 minutes (or 12%
  below the highest high), between 09:15 and 14:55.
- **"Horizontal":** the first candle after that whose body is within a set share of its
  price. A candle without any range (a circuit lock) doesn't count.
- **Entry:** at the next candle's open, against the move: a short sale after a rise, a
  buy after a fall.
- **Exit:** the 4% target, or 15:15.
- **Variants:** nine, from three thresholds for a flat body (0.25%, 0.5%, 1%) and three
  stops (none, 4%, and just beyond the spike's extreme).
- **Costs:** Upstox intraday charges and 0.15% slippage a side, ₹1 lakh a trade.

## Results

| | Development 2022–2024 | Confirmation 2025 – 8 Oct 2026 |
|---|---|---|
| Trades | 746 | 685 |
| Before costs, a trade | +0.29% | +0.45% |
| **After costs, a trade** | **−0.04%** | **+0.11%** |
| ₹ a day (t) | −₹43 (t −0.21) | +₹171 (t 0.45) |
| Profit factor | 0.97 | 1.08 |
| Reached the 4% target | 32% | 32% |

The table is the chosen variant: a flat candle within 1%, no stop.

- **Every variant failed in development**, with t from −0.21 to −0.81 against the 2.5
  needed. Stops didn't help: only once, with the 0.25% threshold, did a stop do a shade
  better than none (−₹69 against −₹89 a day).
- **Slippage decides the sign.**
  - With 0.05% a side, every variant makes 0.06–0.13% a trade, but none has t above 0.9.
  - With 0.30%, every variant loses about 0.3% a trade.
  - Spreads in a stock that has just moved 12% are rarely as tight as 0.05%.

### The owner's claim: "it is going to recover by 4%"

After the spike and the flat candle, by 15:15:

| | Development | Confirmation |
|---|---|---|
| Came back 4% first | 31% | 31% |
| Went on another 4% first | 24% | 23% |
| Neither | 45% | 47% |

- **A recovery is somewhat more likely than a further move**, and the price does fade a
  little on average before costs: +0.3% to +0.45% a trade.
- **But a 4% recovery happens in under a third of spikes.** Usually the stock just
  stays near where it stalled.

### Why it doesn't hold up

- **A few market-wide days decide each period.**
  - On 4 June 2024 (the election results), 57 trades made ₹1.01 lakh. Without that day,
    development lost ₹1.34 lakh.
  - On 7 April 2025 (the tariff sell-off), 113 trades lost ₹1.20 lakh.
- **The worst trades end at a price band.**
  - 168 trades (12%) ended on a candle without a price range, losing 3.7% each. 132 of
    them were short sales of stocks that had kept rising into a lock.
  - In real trading the order to buy those back might not have filled at all. That would
    have meant a short delivery and NSE's auction penalty, which this test doesn't charge.
- **Few large stocks spike like this.** In today's Nifty 200 there were 54 trades in
  development (+0.05% a trade, t 0.05) and 12 in confirmation, too few to show anything.
- **Smaller moves did no better.** The same rule after 8% and 10% moves, on every Nifty 200
  day:
  - development: −0.14% and +0.07% a trade;
  - 2025–2026: −0.75% and −1.1% a trade.
- **Holding back on busy days doesn't rescue it.** Taking only the first 5 signals a day
  made 0.19% a trade in 2025–2026 (t 1.07), after losing 0.15% in development.

## What might be worth a separate test

Without the trades that ended at a price band, the rest made about 0.5% a trade after
costs. That doesn't count as a result: it drops the worst outcomes after the fact.

A real test would need a rule known at entry. One option is to skip short sales in stocks
with a fixed price band (outside F&O) once they are close to it. Both periods here have
now been used, so such a rule would need fresh data, from 9 October 2026 on.

## Limits

- **Intraday restrictions:** brokers block intraday trades in stocks under NSE's ASM and
  GSM surveillance, which includes many small stocks that spike. Those lists' history
  isn't here, so some of these trades couldn't have been placed.
- **Spreads aren't in the candles**, so slippage is assumed and varied.
- **Missing candles:** Upstox had no candles for 1,357 of the 18,070 candidate days. Of
  the 213 ISINs involved, 157 have since been replaced by a new ISIN and 49 no longer
  trade. Another 1,279 days were dropped: listing days, special sessions, or a range
  that disagreed with NSE's bhavcopy.

## Files

- `candidates.py`: the stock-days on which a 12% move was possible, from the bhavcopies.
- `fetch.py`: Upstox's public 5-minute candles for those days.
- `spike.py`: the test. Output in `dev.txt` (all nine variants, development) and
  `confirm.txt` (the chosen variant, confirmation).

The data is not kept in the repository.
