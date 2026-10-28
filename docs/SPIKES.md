# Spike alerts and the spike-reversal paper track

The app watches every liquid NSE stock during the session for a move of **12% or more
within 30 minutes**:

- each one is sent to Telegram, once a stock a day;
- a paper track trades against it, to test the owner's rule forward.

Nothing here places an order. The code is `igs.intraday.spikes`. The page is **Paper
tracking**, **Spike reversal** tab.

## What it watches

- **Stocks:** NSE's EQ series with a median traded value of at least ₹2 crore over the
  previous 20 sessions, from the loaded bhavcopies: about 2,000 stocks.
- **When:** every 5 minutes on weekdays from 09:20 to 15:40, as part of the intraday job
  (`igs intraday`, the `igs-intraday` timer). `igs intraday-spikes` runs one check by hand.
- **Needs** the Upstox access token (Settings, Upstox). Without it the check is skipped.
- **How:**
  1. Upstox's market quotes give every stock's high and low so far today, 500 stocks a
     request, about four requests in all.
  2. Only a stock whose high is at least 12% above its low can have moved 12% within 30
     minutes. The 5-minute candles are fetched for those stocks only: at most 60 a check,
     those with paper trades first.
  3. Only closed candles count.
- **A spike:** a 5-minute close at least 12% above the lowest low of the candles from 25
  minutes before it, or 12% below their highest high. That is the move within 30 minutes
  up to that candle's close.

## The Telegram alert

Each check sends the new spikes in one message to the **spikes chat**
(`IGS_TELEGRAM_SPIKES_CHAT_ID`, or the main chat without one; [DEPLOY.md](DEPLOY.md)):

```
SPIKE ALERT (information, not a call): 1 stock moved 12% or more within 30 minutes.
ABC up 13.1%: ₹99.90 → ₹113.00 by 10:30.
```

- **One alert a stock a day,** the first spike, at any time in the session.
- **A spike found more than 30 minutes late** (Telegram or Upstox unavailable for a while)
  is marked late and not sent.
- **Settings** > Alerts, `intraday_spikes`:
  - `enabled` turns the alerts off;
  - `min_move_pct` changes the 12%;
  - `min_value_cr` changes the ₹2 crore floor.

  The paper track keeps 12% and ₹2 crore whatever they are set to.

## The paper track

The owner's rule: after such a move, trade against it once the candle goes flat, for 4%.

**How the paper trade works:**
- **Entry:** at the first 5-minute candle from the spike whose body is within 1% of its
  open (and has a range), by 14:55. The trade enters at the next candle's open: a short sale
  after a rise, a buy after a fall.
- **Exit:** a 4% target as a limit order, else the 15:15 candle's open. No stop.
- **Costs:** Upstox's intraday charges, and 0.15% slippage a side on the entry and a 15:15
  exit, on ₹1 lakh a trade.

**What the history showed** ([research/spike_reversal](../research/spike_reversal/README.md)):
- **It didn't pass.** On 1,431 spikes in 2022–2026 across all liquid NSE stocks, it made
  about ₹30 a trade.
- **Locked trades did the damage.** The worst trades ended locked at a price band: 12% of
  them, losing 3.7% each, mostly short sales of stocks still rising. In real trading their
  exits might not have filled.

**No short sale near the upper circuit.** The paper track follows the band rule
([PAPER.md](../research/spike_reversal/PAPER.md)'s record B):
- **A short sale** is not taken when the upper circuit (price band) is less than 4% above
  the entry.
- **A buy** is not taken when the lower circuit is less than 4% below the entry.
- **The circuits** come from NSE's security list for the day and the previous close.
- **Stocks with futures** have no fixed circuit and are kept.
- **A trade whose circuit can't be found** is not taken.

**Every spike, for comparison:** the rule as tested, those trades included (record A). It
shows what leaving them out saves or costs.

**Messages:** the paper chat gets each entry. The trades not taken are listed below them,
with the circuit and how close it was. After 15:20 the chat gets the day's results: what
the trades not taken would have made, and both records since the start.

**How it's judged** ([PAPER.md](../research/spike_reversal/PAPER.md), fixed before the
first trade):
- The paper track needs at least 100 trades.
- Its daily P&L, over every session the watcher ran, must then be above zero with a
  t-statistic of 2 or more.
- Until then the tab says how many trades it has.
- A pass makes it a candidate for real orders, which would need the owner's decision and its
  own change.

## Limits

- **Intraday restrictions:** brokers don't allow intraday trades in stocks under NSE's
  surveillance measures (ASM and GSM). The paper track doesn't know those lists, so some of
  its trades couldn't be placed for real.
- **Live candles** can differ slightly from Upstox's historical ones that the test used.
- **Busy days:** on a crash or a results day with more than 60 candidates, a spike can be
  found a check late. The paper trade is still recorded from the right candles, but its
  alert may be late or marked late.
