# Spike recovery with machine learning: the validation result and the choice

Written on 29 October 2026, after `model.py validate` (output in `validate.txt`) and before
the test period was run.

## Data

- 240,767 usable stock-days, the same as `research/spike_sizes`.
- 103,935 decision points: 71,627 distinct candles, since one candle can reach several
  thresholds.
- Trained on 2022–2023 (19,374 candles); validated on 2024 (21,122).

## The maximum recovery, 2022–2024 (the descriptive answer)

The largest move back against the spike from the next candle's open, before 15:15, before
costs:

| Move within 30 minutes | Points | Median | Mean | Reached 1% | 2% | 3% | 4% | 5% | Median minutes to it |
|---|---|---|---|---|---|---|---|---|---|
| 5% | 27,294 | 2.4% | 2.9% | 78% | 57% | 39% | 25% | 15% | 55 |
| 6% | 15,004 | 2.6% | 3.2% | 80% | 62% | 44% | 30% | 19% | 50 |
| 7% | 8,565 | 2.9% | 3.5% | 81% | 65% | 48% | 34% | 23% | 50 |
| 8% | 5,102 | 3.1% | 3.8% | 82% | 67% | 51% | 37% | 27% | 48 |
| 10% | 1,872 | 3.3% | 4.3% | 81% | 67% | 53% | 41% | 32% | 45 |
| 12% | 794 | 3.3% | 4.7% | 81% | 66% | 53% | 43% | 35% | 35 |
| 15% | 246 | 3.4% | 4.7% | 76% | 64% | 53% | 41% | 35% | 25 |

- **The recovery grows with the move up to about 10%, then levels off,** at a median of
  about 3.3%.
- **Bigger moves give back their recovery sooner:** 25 minutes for a 15% move against 55
  for a 5% one.
- **This is the best the price did, not a trade:** it doesn't say the price went there
  before moving further against the trade.

## The models, 2024

**The recovery model:**
- The correlation between its predicted and actual recovery was 0.42.
- By tenth of its prediction, the actual recovery ran from 1.9% (lowest tenth, predicted
  1.75%) to 5.8% (highest, predicted 5.3%).
- **It ranks spikes by recovery well.**
- Its main inputs:
  - the previous session's return (19% of the gain);
  - the move from the previous close (13%);
  - the decision candle's range (9%);
  - the time of day (7%).

**The trading rule, by gate**, at 0.15% slippage a side:

| Gate | Trades | A trade, after costs | Rs a day | t |
|---|---|---|---|---|
| 0% | 9,521 | +0.12% | +4,753 | 1.02 |
| 0.25% | 7,219 | +0.28% | +7,944 | 1.87 |
| **0.5%** | 4,918 | +0.46% | +9,143 | **2.21** |
| 0.75% | 3,183 | +0.67% | +8,520 | 2.07 |
| 1.0% | 2,023 | +0.95% | +7,697 | 1.94 |

**The candidate:** a gate of 0.5%. Its validation t is 2.21.

**It does not pass:** the protocol needs a validation t of at least 2.5.

## Things the validation showed

- **One day:** 4 June 2024, the election results, had 615 of the 4,918 trades and made
  Rs 8.86 lakh of the year's Rs 22.8 lakh.
- **Buys after falls did better than short sales after rises:** +1.33% a trade on 1,271
  buys, against +0.16% on 3,647 short sales.
- **Locked exits:** 347 trades (7%) ended on a candle without a price range, losing 3.4%
  each.
- **Slippage:** at 0.05% a side the candidate made +0.62% a trade (t 2.84); at 0.30%,
  +0.22% (t 1.16).
- **The fixed rules on the same points**, fade at once, no stop:
  - every 5% move lost;
  - the best was a 12% move with a 1% target: +0.37% a trade, t 2.84, 415 trades.

## The choice

The gate of 0.5%, with the models retrained on 2022–2024, is run once on 1 January 2025 –
8 October 2026, for the record. It has already failed the validation bar, so the study
does not pass whatever the test shows, and nothing is built into the app.
