# Spike sizes: the development result and the choice

Written on 28 October 2026, after `sizes.py dev` (output in `dev.txt`) and before the
confirmation period was run.

## Data

- **279,490 candidate stock-days:**
  - 20,787 had no candles from Upstox;
  - 17,936 were dropped: not a full session, or a range off the bhavcopy's by more than
    0.5%;
  - **240,767 were used,** 133,520 of them in development (2022–2024).

## Development, 2022–2024

**Before costs, from an entry right after the move** (the direct answer to the question):

| Move within 30 minutes | Moves | 3% back first | 3% further first | Neither by 15:15 |
|---|---|---|---|---|
| 5% | 27,294 | 35% | 32% | 33% |
| 6% | 15,004 | 40% | 33% | 28% |
| 7% | 8,565 | 42% | 33% | 24% |
| 8% | 5,102 | 45% | 33% | 23% |
| 10% | 1,872 | 46% | 34% | 19% |
| 12% | 794 | 47% | 30% | 23% |
| 15% | 246 | 48% | 17% | 33% |

The bigger the move, the more often it gave back 3% before going 3% further.

**After costs** (0.15% slippage a side and the charges):

- **Trading with the move loses** in all 28 of those variants, by 0.4–1.8% a trade.
- **Against the move, entering at once (N) with no stop** does better the bigger the move:
  −0.10% a trade at 5%, +0.04% at 6%, +0.11% at 7%, +0.25% at 8%, +0.26% at 10%, +0.49% at
  12% and +0.65% at 15%.
- **Waiting for a flat candle (F)**, the owner's rule in the spike-reversal study, loses
  most of that. At 12%, for example, it made −0.09% against N's +0.49%. The move gives
  back most of what it will within the first candles.
- **A 3% stop** did worse than none in 13 of the 14 fade variants.

**The candidate** (highest development t): a 15% move, entering at once, against the move,
no stop.
- 246 trades, about one every three sessions: +0.95% a trade before costs, +0.65% after.
- Rs 214 a day; t 3.10; profit factor 1.72; 52% reached the target.
- **It meets the development bar (t ≥ 3.0), only just.**

The same variant at 12% made +0.49% a trade (t 2.60), and at 10% +0.26% (t 1.34).

## Warnings before the confirmation

- **Locked exits:** 45 of the 246 trades (18%) ended on a candle without a price range,
  losing 2.2% each. 36 of them are short sales of stocks still rising into their upper
  band. A real order to close them might not have filled, which would mean NSE's
  short-delivery auction. This test charges nothing for that.
- **Slippage matters:** at 0.30% a side the candidate made +0.48% a trade (t 2.27); at
  0.05%, +0.78% (t 3.76).
- **Few large stocks:** only 15 of the trades were in today's Nifty 200.

## The choice

A 15% move, entering at once, against the move, no stop (15%, N, fade, S0) is run once on
the confirmation period, 1 January 2025 – 8 October 2026. It passes with a mean above zero,
a t-statistic of at least 2.0, and at least 100 trades.
