# Study D: intraday picks the evening before, October 2026

Picks each day's intraday trades after the previous close, so orders can be ready for the
09:15 open, and holds them to 15:15. Fixed in `PROTOCOL.md` (SHA-256 and Amendment 1 in
`PROTOCOL.sha256`) before any feature was computed; one candidate chosen on 2023-2024
(`CHOICE.md`); holdout computed once.

## Result: passed, narrowly; a candidate for forward paper calls

LightGBM averaged over five seeds, on 21 features known by the previous close plus the
day of the week. Each day: up to three buys and three short sells whose predicted move is
at least 0.15%. Entry at the 09:15 open, exit at the 15:15 open, 0.05% slippage a side and
Upstox charges on Rs 1 lakh.

| Holdout, Jan 2025 - 8 Oct 2026 | |
|---|---|
| Trades | 1,540 (3.5 a day) |
| Per trade, before / after costs | +0.32% / +0.14% |
| Profit factor | 1.16 |
| Net per day (Rs 1 lakh a trade) | Rs 488 (t 2.02) |
| 2025 / 2026 per day | Rs 646 / Rs 283 |
| Buys / short sells, per trade after costs | -0.04% / +0.22% |
| At 0.02% slippage (reported) | Rs 695 a day (t 2.88) |
| Each seed alone, Rs a day | 361, 382, 388, 480, 684 |

It ranks stocks better than the 09:45 model: daily rank IC 0.044 (t 7.5) in the holdout
and 0.056 (t 10.5) in development, against Study B's 0.019 and 0.034. The longer
open-to-15:15 window has bigger moves to rank, which helps pay the higher opening costs.

## Caveats

- **Barely over the bar.** t 2.02 against 2.0, on a period seen through Studies B and C.
  Every seed was profitable, so it isn't one lucky draw, but the evidence is modest.
- **The profit is all short sells,** as with Study B. Buys lost 0.04% a trade.
- **Survivorship:** the universe is today's Nifty 200 members. That favours buying, not the
  short sells this model makes money on.
- **Opening fills.** Results assume the 09:15 candle's open, with 0.05% slippage. Real
  market orders at the open can do worse in thin names. At 0.02% it is clearly stronger.
- **After the holdout** (9-23 October, 10 sessions, too few to judge): -Rs 2,160 a day,
  nearly all on buys (-2.05% a trade). The 09:45 model also lost heavily in those days.

## Next step, per the protocol

Forward paper calls with the rules frozen: each evening, the next day's up-to-three buys
and short sells, recorded and closed at 15:15 like the 09:45 model's paper record. Nothing
is traded with real money from this study.

## Files

| Purpose | Files |
|---|---|
| Features at the previous close | `features.py` |
| Walk-forward, candidates, holdout | `model.py` (`dev.txt`, `holdout.txt`) |

Data: Study C's download (`research/seq/fetch.py`). Runs in the repository's `.venv` with
scikit-learn; the walk-forward took about 25 minutes on CPU.
