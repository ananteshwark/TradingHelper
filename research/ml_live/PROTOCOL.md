# ML intraday calls with real orders: the exit and entry they can actually have

Written 2026-10-15, before any of these variants was run.

**Why.** The owner asked to trade the ML intraday calls (intraday-ml-v1) with real money,
approved on the page or by a Telegram reply. Two things make a real order differ from the
tested one:

- **Exit.** The tests exit at the 15:15 candle's open. Since NSE's closing auction session
  started on 3 August 2026, Upstox squares off intraday positions in CAS-eligible (F&O)
  stocks at 15:10, for a fee, and refuses new intraday orders in them after 15:09. Most
  Nifty 200 stocks are F&O stocks. The app's own square-off must come before that: 15:00.
- **Entry.** The calls are decided between 09:45 and 09:55 and sent by Telegram; an order
  goes in only after the owner approves. The tests enter at the 09:50 open.

The real order also carries the stop chosen in research/winners (half the 14-day ATR from
the entry), so every variant here has it.

**Data and calls.** As research/winners: the frozen walk-forward LightGBM predictions
(k = 3, 0.15% gate), Upstox 5-minute candles of the Nifty 200 (n200_long), Upstox intraday
charges on Rs 1 lakh a trade and 0.02% slippage on every market entry and exit. A candle
that reaches the stop exits at the stop, or at its open when it opens beyond it; the entry
candle counts.

**Variants.** All with the stop:
- L0: entry at the 09:50 open, exit at the 15:15 open (research/winners E0/X1, reference);
- L1: entry at the 09:50 open, exit at the 15:00 open;
- L2: entry at the 09:55 open, exit at the 15:00 open;
- L3: entry at the 10:00 open, exit at the 15:00 open.

**Periods.** 2023-2024 and January 2025 to 8 October 2026, each reported in full. Both
were used before (the model's own test, and the stop's), so this is a feasibility check,
not a new test of the model; the forward record is the real one.

**What is decided by it, fixed now.**
- Real-money orders exit at 15:00 only if L1 earns more than zero a trade after costs in
  both periods. Otherwise they are not built, and the owner is told why.
- Approvals are accepted until the latest of 10:00 (L3), 09:55 (L2) or 09:50 (L1) whose
  variant earns more than zero a trade after costs in both periods.

Reported for each: trades, average % a trade after costs, win rate, profit factor, rupees
a day, t of daily rupees, maximum drawdown, worst trade, short sells and buys apart.
