# ML intraday calls with real orders: the exit and entry they can have

The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed before `live.py`
was first run.

## Why

The owner asked to trade the ML intraday calls (intraday-ml-v1) with real money,
approving each one on the page or by a Telegram reply. A real order can't do two things
the tests did.

- **Exit at 15:15.** Since NSE's closing auction session began on 3 August 2026, Upstox
  squares off intraday positions in stocks in that auction (F&O stocks) at 15:10, for a
  fee. It refuses new intraday orders in them after 15:09
  ([Upstox help](https://upstox.com/help-center/252251/)). Most Nifty 200 stocks are F&O
  stocks, so the app's own square-off must come first: 15:00.
- **Enter at 09:50 every time.** The calls are decided between 09:45 and 09:55. An order
  goes in only after the owner approves.

Every variant carries the stop that real orders have: half the 14-day ATR from the entry,
chosen in research/winners.

## Results

The same frozen calls, candles, charges on ₹1 lakh a trade and 0.02% slippage a side as
research/winners. Full tables are in `dev.txt` and `holdout.txt`.

| Variant | Entry | Exit | 2023–2024, % a trade after costs (t) | 2025–Oct 2026 (t) |
|---|---|---|---|---|
| L0 (research/winners E0/X1) | 09:50 | 15:15 | +0.064 (1.34) | +0.100 (1.93) |
| L1 | 09:50 | 15:00 | +0.046 (1.02) | +0.082 (1.68) |
| L2 | 09:55 | 15:00 | +0.033 (0.75) | +0.061 (1.31) |
| L3 | 10:00 | 15:00 | +0.014 (0.32) | +0.057 (1.29) |

- Short sells carry it, as before: +0.08% a trade in 2023–2024 and +0.15% in 2025–2026
  under L1. Buys are about zero.
- The worst trade is −8.4% and −4.8% in the two periods. The drawdown is ₹46,000–58,000
  on ₹1 lakh a trade.

## What it decides, by the rules fixed in advance

- **Real orders are built, exiting at 15:00.** L1 is above zero in both periods.
- **Approvals are accepted until 10:00.** L3 is above zero in both periods.

Neither variant is strong, and delay costs. From a 09:50 to a 10:00 entry, the edge falls
from +0.05% to +0.01% a trade in 2023–2024 (₹14 on ₹1 lakh). In 2025–2026 it falls from
+0.08% to +0.06%. No variant reaches t = 2. Both periods were used before, so this is a feasibility check, not a new
test of the model. The forward record, paper and real, is the test.
