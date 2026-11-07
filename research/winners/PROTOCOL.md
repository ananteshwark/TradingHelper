# What profitable traders do differently: tests on the ML intraday calls

Written 2026-10-09T11:30:37Z, before any of these variants was run.

The evidence (SEBI FY23 cash-segment study; Barber, Lee, Liu & Odean 2014; Garvey & Murphy;
Locke & Mann; Coval & Shumway) points to these habits among the few skilled day traders:
lower costs (fewer, larger trades), more passive (limit) orders, cutting losers quickly,
not chasing after losses, concentration and trading on information. The first three can be
tested on the frozen ML intraday calls (LightGBM, k = 3, 0.15% gate; walk-forward
predictions in research/ml).

**Variants.** Entry, each from the 09:50 candle:
- E0: market order at the 09:50 open (the app's paper rule);
- E1: limit 0.10% better than the 09:40 candle's close (the price the model saw), resting
  until the 10:25 candle closes, filled at the limit (or the candle's open when it opens
  through it), otherwise no trade;
- E2: as E1, 0.25% better.

Exit:
- X0: the 15:15 open;
- X1: a stop 0.5 x the 14-day ATR from entry, else the 15:15 open;
- X2: a stop 1.0 x the ATR.

A candle that reaches the stop exits at the stop, or at its open when it opens beyond it.
The entry candle counts.

**Costs.** Upstox intraday charges on Rs 1 lakh. 0.02% slippage on market orders (E0
entries, all exits), none on limit entries.

**Choice.** The variant with the highest t-statistic of daily net P&L in 2023-2024.

**Confirmation.** 2025 to October 2026, run once for the chosen variant and the baseline
E0/X0. That period was already used once to test the model itself, so this is a weaker test
than the first; the forward paper record is the real one.

**Trade size** is a calculation, not a test: costs as % of a trade, by size.
