# Spike alerts and the spike paper tracks

The app watches every liquid NSE stock during the session for sharp moves within 30
minutes:

- **12% or more:** each one is sent to Telegram, once a stock a day, and the spike-reversal
  paper track trades against it, to test the owner's rule forward.
- **5% or more:** a fixed model picks the ones it expects to come back, and the spike-picks
  paper track records them, buys and short sales apart ([Spike picks](#spike-picks)). Each
  pick is sent to Telegram.

Nothing here places an order. The code is `igs.intraday.spikes` and
`igs.intraday.spike_ml`. The page is **Paper tracking**, **Spike reversal** and **Spike
picks** tabs.

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
     those with paper trades first. The spike picks add the stocks with a range of 5% or
     more, up to 150 stocks a check in all; each stock's candles are fetched once.
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

## Spike picks

The spike-recovery study ([research/spike_ml](../research/spike_ml/README.md)) trained a
model on 2022–2026 spikes:
- It predicts how much a spike will come back, and the result after costs of trading
  against it.
- Overall it didn't pass: +0.06% a trade in 2025–2026.
- Its buys after falls made +0.60% a trade (t 2.23), while its short sales lost 0.09%. That
  split was seen after the fact, so this paper track tests it from here, buys and short
  sales apart.

**How a pick is made** ([PAPER.md](../research/spike_ml/PAPER.md), fixed before the first
pick):
- **Stocks:** each one trading ₹2 crore a day whose range today is 5% or more is checked
  every 5 minutes.
- **Decision points:** when the stock's move within 30 minutes first reaches 5, 6, 7, 8,
  10, 12 or 15%, the model predicts the after-cost result of trading against it with a
  target of 1%, 2%, 3%, 4% and 5%.
- **The pick:** the first decision point whose best prediction is above 0.5%, with that
  target. It is a buy after a fall, or a short sale after a rise.
  - Entry at the next candle's open.
  - The target, or out at the 15:15 open. No stop.
  - Charges and 0.15% slippage a side, on ₹1 lakh.
- **One pick a stock a day.**
- **Too late:** a pick found more than 10 minutes after its candle closed (a busy day, or
  Upstox down) is recorded as missed, not traded.
- **No short sale near the upper circuit:** one whose upper circuit is less than 4% above
  the price isn't taken. Every short sale is kept beside the record for comparison.
- **The model** is `config/models/spike_ml_v1.json`, trained once and fixed; it runs
  without LightGBM. `research/spike_ml/export_model.py` rebuilt it and checked two things:
  that the app computes the same features as the study, and that the trees give LightGBM's
  predictions.

**Messages:**
- **The spikes chat** gets each new pick: the stock, the move, what the model expects and
  its target. Short sales near the circuit are listed as not taken.
  - Turn these off with Settings > Alerts, `intraday_spikes`, `model_picks`.
- **The paper chat** gets the day's results after 15:20.

```
SPIKE PICKS (the model, on paper): 1 new.
BUY ABC after a 6.2% fall within 30 minutes (₹200.50 → ₹188.00 by 10:15). The model expects
+0.9% after costs with a 3% target (about ₹193.64); paper entry at the 10:15 open, out by
15:15.
```

**How it's judged:** buys and short sales separately, each after 100 trades. Its daily P&L
over every session the watcher ran must be above zero with a t-statistic of 2 or more. A
pass makes it a candidate for real orders, which would need the owner's decision.

**Limits, besides those above:**
- **The crowd features** (how many stocks spiked earlier that day) count only the stocks
  checked so far, which is fewer on a busy day.
- **Exits are settled after 15:20,** so a pick's result is known only then. If the watcher
  stops before that, the next day's first check settles it from that day's candles.
