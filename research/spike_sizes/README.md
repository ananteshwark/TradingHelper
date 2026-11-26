# Spike sizes: which move within 30 minutes is best for a 3% gain?

The owner asked: among stocks that move more than 5%, which size of move is best for a 3%
gain?

The test:
- **Moves:** every liquid NSE stock, 2022 – 8 October 2026, moving 5%, 6%, 7%, 8%, 10%,
  12% or 15% within 30 minutes.
- **Trades:** against the move or with it, entering at once or at the first flat candle,
  with or without a 3% stop, and a 3% target, out at 15:15 otherwise.
- **Costs:** Upstox charges and 0.15% slippage a side on ₹1 lakh.

The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed (34e432e) before any
trade was simulated. The choice (`CHOICE.md`) was committed (eca688c) before the
confirmation period was run.

**Verdict: no size of move gives a reliable 3% gain after costs.**
- The best rule of 2022–2024 lost money in 2025–2026: fade a 15% move at once, no stop.
- Trading with the move lost in every version, in both periods.
- Nothing is built into the app.

## How often does a move give back 3% first?

The direct answer to the question, before costs. After the move, from the next candle's
open, the first of 3% back (against the move) and 3% further (with it), by 15:15:

| Move within 30 minutes | 2022–2024: back / further / neither | 2025 – Oct 2026: back / further / neither |
|---|---|---|
| 5% | 35% / 32% / 33% | 34% / 33% / 33% |
| 6% | 40% / 33% / 28% | 37% / 36% / 27% |
| 7% | 42% / 33% / 24% | 39% / 38% / 23% |
| 8% | 45% / 33% / 23% | 41% / 39% / 20% |
| 10% | 46% / 34% / 19% | 43% / 38% / 19% |
| 12% | 47% / 30% / 23% | 40% / 36% / 24% |
| 15% | 48% / 17% / 33% | 35% / 31% / 34% |

- **Bigger moves gave back 3% first more often**, most clearly in 2022–2024.
- **In 2025–2026 the gap narrowed:** for 15% moves it fell from 48% against 17% to 35%
  against 31%.
- **A 5% move is close to a coin toss** in both periods.

There were about 27,000 moves of 5% in 2022–2024 and about 250 of 15%. At 5% that is
about 37 stocks a day; at 15%, one every three sessions.

## After costs

**Against the move, entering at once, no stop**, a trade after costs:

| Move | 2022–2024 | 2025 – 8 Oct 2026 |
|---|---|---|
| 5% | −0.10% | −0.15% |
| 6% | +0.04% | −0.11% |
| 7% | +0.11% | −0.11% |
| 8% | +0.25% | −0.06% |
| 10% | +0.26% | +0.06% |
| 12% | +0.49% | +0.04% |
| **15%** (the chosen rule) | **+0.65%**, t 3.10, 246 trades | **−0.24%**, t −0.72, 238 trades |

- **The chosen rule failed its confirmation.** Before costs it made +0.95% a trade in
  2022–2024 and +0.09% in 2025–2026.
  - In 2025–2026 its short sales after rises lost 0.50% a trade after costs; its buys after
    falls made +0.40% on 69 trades (t 0.68).
- **One day decided much of 2025–2026:** on 7 April 2025 (the tariff sell-off) it took 44
  trades and lost ₹45,751.
- **Trading with the move lost in all 28 of its variants in 2022–2024** (−0.43% to −1.79% a
  trade) **and in 27 of 28 in 2025–2026.** The exception made +0.11% a trade (t 0.33).
- **Waiting for a flat candle,** the owner's rule in the spike-reversal study:
  - in 2022–2024 it gave up most of the fade, since the move gives back most of what it will
    in the first candles;
  - in 2025–2026 it did slightly better than entering at once (+0.12% a trade at 10%,
    t 1.03), but nothing reached significance.
- **A 3% stop** made results worse in 13 of 14 fade variants in 2022–2024, and in all 14 in
  2025–2026.
- **Slippage:** at 0.05% a side, the best variant at each size made at most +0.27% a trade
  in 2025–2026 (t 2.47 at 10%, on the flat candle). That was found after looking, so it
  doesn't count. At 0.30% every one lost.

## Why it doesn't hold

- **The edge is small and unstable.** Before costs, fading at once made +0.23% to +0.95% a
  trade in 2022–2024, depending on the size of the move, and +0.09% to +0.37% in
  2025–2026. A round trip with slippage and charges costs about 0.33%.
- **Price bands:**
  - 35 of the chosen rule's 238 trades in 2025–2026 (and 45 of 246 in 2022–2024) ended
    locked at a band, losing 2.2–2.8% each. In 2022–2024, 36 of the 45 were short sales into
    an upper band;
  - in real trading those exits might not have filled.

  This is the same problem the spike-reversal study found. The band rule now on paper
  (`research/spike_reversal/PAPER.md`) tests leaving such trades out.
- **Large stocks rarely move like this:** today's Nifty 200 had 15 of the rule's trades in
  2022–2024 and 3 in 2025–2026.

## Limits

- As in the spike-reversal study:
  - brokers' intraday restrictions (ASM and GSM) aren't replayed;
  - spreads are assumed, not measured;
  - the universe includes delisted stocks, but 20,787 of 279,490 candidate days had no
    candles from Upstox.
- **12% and 15% moves** overlap the spike-reversal study's trades and periods.

## Files

- `candidates.py`: the stock-days with a range of 5% or more, from the bhavcopies.
- `fetch.py`: Upstox's public 5-minute candles for them.
- `prepare.py`: the usable days in one table.
- `sizes.py`: the test. Output in `dev.txt` and `confirm.txt`.

The data (3 GB) is not kept in the repository.
