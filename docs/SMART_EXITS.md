# Exits that adapt during a trade: tested

The owner asked whether an AI could watch open trades in real time and square them off at
the right moment, to keep more profit or lose less. This tests the usual ways of doing that
on the app's data (`research/exits/`).

## What the app does now

- **Real intraday orders:** every one goes to Upstox as a GTT order with its exits attached:
  a stop 1% beyond the entry and the call's target.
  - Upstox holds them, so they trigger even if the app's server is down.
  - The app checks their state and reports fills and the result after charges.
  - Upstox squares off intraday positions still open near the close.
- **The ML paper calls:** they have no stop and close at 15:15.

## The test

**Trades:** the ML intraday calls, three buys and three short sells a day, entered at the
09:50 open.

**Prices and costs:**
- Upstox 5-minute candles.
- Upstox intraday charges on ₹1 lakh, plus 0.02% slippage on the entry and on every exit.

**Periods:**
- 2023–2024 to choose between variants.
- January 2025 – 8 October 2026 to confirm, run once. This period was already used to test
  the model, so this is a weaker test than a fresh one.

**Rules:** written down before any result was seen (`research/exits/PROTOCOL.md`). Each
idea had to beat the best simple rule found so far: a fixed stop at half the stock's
average daily range (ATR) from the entry.

| Exit | 2023–2024: a trade after costs (t), worst drawdown | 2025–2026: a trade after costs (t), worst drawdown | Result |
|---|---|---|---|
| None, out at 15:15 (the paper rule) | +0.03% (0.6), ₹69,287 | +0.12% (2.2), ₹65,299 | for comparison |
| **Fixed stop at half the ATR** | +0.06% (1.3), ₹57,347 | +0.10% (1.9), ₹55,890 | the rule to beat |
| Trailing stop, half or one ATR behind the best price | +0.05% (1.2) / +0.03% (0.6) | not run | failed on 2023–2024 |
| Stop moves to the entry once the trade is up one ATR | +0.06% (1.4), ₹57,430 | +0.10% (2.0), ₹52,809 | no real difference |
| Out at 12:00 if losing | +0.08% (1.8), ₹47,051 | +0.08% (1.5), ₹53,946 | worse in 2025–2026 |
| **Machine-learning exit**, checked every 30 minutes | +0.01% (0.3) / +0.03% (0.9) | not run | failed on 2023–2024 |
| Close all trades when the day is down 1% a trade on average | +0.07% (1.5), ₹55,077 | +0.09% (1.8), ₹61,363 | smaller worst day, not a smaller drawdown |

Drawdowns are the largest fall in rupees for six ₹1 lakh trades a day.

**The machine-learning exit model:**
- **Training:** about 230,000 to 640,000 examples from every stock and day, refitted each
  year on past years only.
- **Inputs:**
  - the trade's progress so far;
  - its best and worst moves;
  - its recent 5-, 15- and 30-minute moves;
  - its distance from the day's VWAP;
  - the time left;
  - the Nifty's move.
- **Each check:** it predicted whether holding to 15:15 would beat exiting now.

## What it found

- **No adaptive exit beat the fixed stop.** The fixed stop at half the ATR limited the
  worst trade to about −5% in 2025–2026; in 2023–2024 a price gapped through it once, to
  −8%. Every rule that tried to time the exit more cleverly was the same or worse after
  costs.
- **The machine-learning exit did worst.** It closed 70–90% of trades early and cut the
  average from +0.06% to +0.01–0.03% a trade.
  - Once a trade is open, the rest of the day's move is close to unpredictable from its
    path so far.
  - Exiting on a forecast mostly cuts winners short.
- **Trailing stops gave up winners.** Half an ATR behind the best price closed half the
  trades on ordinary swings.
- **"Out at noon if losing" looked best in 2023–2024 but not in 2025–2026.** In the later
  period many trades losing at noon recovered by 15:15.
- **A daily loss limit cut the worst day by a third,** from ₹10,358 to ₹6,957. It did not
  shrink the worst losing stretch, and it cost about 0.01% a trade.
- **Not stopping at all made the most in 2025–2026,** at the price of a −16% trade and the
  deepest drawdown. The fixed stop is a trade of a little return for much less tail risk.

## What this means here

1. **Keep a fixed stop placed with the broker at entry,** as the app's real orders already
   do. It protects even if the server, the network or the app fails. The test's stop, half
   a typical day's range, was about 1.35% for the median stock; the real orders' 1% is in
   the same range.
2. **Don't add an AI or machine-learning exit.** On this data it lowered profits, and a
   language model watching ticks would be slower, cost money on every check, and couldn't
   be tested honestly on the past.
3. **A daily loss limit is optional,** as a personal safety rule rather than a source of
   profit: it trims the worst days at a small cost.
4. **Exits manage risk; the profit has to come from the entry.** This matches the earlier
   finding that a stop "buys safety, not profit" ([WINNING_TRADERS.md](WINNING_TRADERS.md)).
