# Development choices (PROTOCOL.md)

Written on 14 November 2026 from `dev.txt`, before any holdout or confirmation run. The
rule in each case is the protocol's: the candidate with the highest t-statistic of its
difference from the baseline, provided its net return (Study A: a trade; Study B: a year)
is at least the baseline's; a family with no such candidate fails.

## Study A: trading (July 2023 – 2024, seed 7)

| | 09:45 calls | Evening picks |
|---|---|---|
| **F**, the order rule | 1,561 trades, +0.065% a trade, ₹275 a day | 1,594 trades, +0.020% a trade, ₹80 a day |
| **A1**, veto | **VR**: +0.134% a trade, t −0.04 (V20 +0.034%, V33 −0.013%: below F) | **VR**: +0.182% a trade, t 1.63 (V20 t 0.41, V33 t 0.10) |
| **A2**, early exit | **fails**: E0 +0.028%, E10 +0.059%, C50 +0.060%, C60 +0.062%, all below F's +0.065% | **fails**: E0 −0.024%, E10 −0.026%, C50 −0.002%, C60 +0.018%, all below F's +0.020% |
| **AB** | not run (A2 failed) | not run (A2 failed) |

**For the holdout:** `m0945=VR evening=VR`. Two tests against t ≥ 2.0.

Seeds 8–10 put VR's t at 0.22, 0.53, 0.12 for the 09:45 calls and 2.25, 1.42, 1.69 for
the evening picks.

## Study B: investing (months ending 2016–2020)

**M reproduces:** 18.4% a year after costs, before tax, as research/momentum_upgrades
(18.4%).

| | Return a year | Max drawdown | Excess a month | t |
|---|---|---|---|---|
| **M** | 18.4% | −38.8% | | |
| **B1:** BM 14.1%, BH 11.0%, BV 14.2%, BX 15.5% | all below M | | | |
| **B2: XM3** | 23.1% | −34.7% | +0.287 points | 2.09 |
| B2: XR | 21.4% | −31.2% | +0.141 | 0.83 |
| B2: XM5 | 18.7% | −38.7% | +0.019 | 0.69 |

**B1 fails on development:** each of the four gave up 3 to 7 points a year.

**For confirmation:** `B1 - B2 XM3`.

## What was seen before these choices

Every choice above follows from the development numbers by the rule. Three things were
nevertheless seen before them, and are recorded here:

1. **Study A's baseline in the holdout:** checking that F reproduces research/ml_live's
   L1 (+0.046% a trade in 2023–2024, as there), the same check printed F for 2025–2026:
   - the 09:45 calls: +0.079% a trade (ml_live: +0.082%);
   - the evening picks: −0.062% a trade, with 27% stopped.

   F is the baseline, not a candidate, and the protocol had said it would report it.
2. **Study B's first development run charged no trading costs.** The rebalance's 0.01
   tolerance stopped its iteration before any cost was taken, from a book of 1.0; M came
   out at 20.8%. The fix starts the book at ₹10 lakh, as upgrades.py does. The candidates'
   ranking was the same in both runs: B1 all below M, XM3 the highest t.
3. **Part 1 on development** (`dev.txt`): the drivers of a stop-out.

## Implementation notes fixed before any run

These are in each script's docstring: `paths.py`, `models_a.py`, `drivers.py`,
`momentum.py`. The ones that matter most:
- **One A2 model** across the five checkpoints, with the minutes since entry among its
  parameters.
- **`own_*` parameters** (the stock's own gap and volume before the entry) are shown only
  as exploratory, outside the models.
- **B2's sale** is at the session's close, paying 0.195%; the money is held as cash.
