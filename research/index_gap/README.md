# The Nifty200 Momentum 30 rebuilt in the engine, and the gap to the backtest

The owner asked for the momentum index to be rebuilt in the app's engine, and for the gap
to be found between the index and the Rankings order's backtest. That backtest is M in
`research/momentum_upgrades`: 38% a year after costs in 2021–2026.

The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed (832d7b1) before
anything was computed.

**Verdict:**
- **The engine is sound.** Rebuilt from its own price data with NSE's published rules, the
  index made 16.0% a year against the real index's 14.3%, with monthly correlation 0.91.
- **The gap is real in the data.** M's largest single moves are all genuine rallies.
- **Where the gap comes from:** three choices M makes and the index doesn't, in a period
  that rewarded all three:
  - equal weights;
  - the 500 most-traded stocks (small and mid caps) rather than the 200 largest;
  - rebalancing every month rather than every six months.

## The two figures, on the same months

The figures cover 31 December 2020 to 30 September 2026, 69 months, as price returns.

- **The real index:** the Nifty200 Momentum 30 price index made **14.3% a year**, against
  12.0% for the Nifty 500.
- **The 9.3% quoted before** was a different window (the five years from September 2021) and
  probably the total return index.
- **The like-for-like gap** to M's 38.0% is 24 points a year.

## The steps from the index to M

Each step changes one thing, in the order the protocol fixed (`gap.txt`):

| Step | A year | Change |
|---|---|---|
| A. The Nifty200 Momentum 30 (price) | 14.3% | |
| B0. Rebuilt with NSE's rules | 16.0% | +1.7 against A; monthly correlation 0.91, tracking error 8.4% |
| B1. Equal weights, not market cap × score | 22.0% | **+5.9** |
| B2. 20 stocks, not 30 | 21.5% | −0.5 |
| B3. Monthly at the signal's close, not semi-annual a month late | 27.8% | **+6.3** |
| B4. M's score, not NSE's | 27.9% | +0.1 |
| B5. M's universe, the 500 most traded, not the 200 largest | 40.7% | **+12.8** |
| B6. Costs, 0.195% each way (= M) | 38.0% | −2.7 |

**The universe first, as a check:**

| Step | A year | Change |
|---|---|---|
| U. NSE's rules on the 500 most traded | 16.8% | +0.8 |
| U1. Equal weights | 32.7% | **+15.9** |
| U2. 20 stocks | 29.3% | −3.5 |
| U3. Monthly at the signal's close | 42.9% | **+13.6** |
| U4. M's score (= B5) | 40.7% | −2.2 |

**What the two orders agree on:**
- **The universe matters only with equal weights.** Weighted by market cap, the 500
  most-traded and the 200 largest give much the same result, because the money stays in the
  largest stocks either way. Equal weights move it into the small and mid caps the wider
  universe adds.
- **Equal weights and monthly rebalancing account for most of the gap** in either order.
  Each is worth roughly 6 to 16 points a year, depending on what comes before it.
- **The momentum score itself barely matters.** NSE's score and M's give much the same.

## The rebuilt index

B0 stands in for NSE's index with four approximations.

- **The universe:** the 200 largest companies by full market cap, standing in for NSE's
  Nifty 200 list.
- **The weights:** full market cap rather than free float.
- **Share counts:** from NSE's 30 October 2026 issue sizes. Stocks delisted before then have
  none: 3 to 14 of the 200 most traded at each month end (median 7).
- **The buffer:** a guessed rank buffer (with no buffer it made 15.5%).

Even so, it tracks the real index closely (correlation 0.91) and made 1.7 points a year more.
That is within what the approximations explain.
- **No large bias in the engine's data:** the engine doesn't turn a 14% strategy into a 38%
  one by bad prices.
- **By year** it is close in most years, with the largest differences in 2021 (+66% against
  +52%) and 2023 (+22% against +41%).

## How much of M is momentum, and how much is the period

Two checks beyond the protocol, written after its results were seen (`extra.py`,
`extra.txt`). They are not part of the test.

- **The universe alone:** all of the 500 most-traded stocks held in equal weights, rebalanced
  monthly, with no momentum selection, made **20.3%** a year. The 200 largest held the same
  way made 17.2%; the market-cap-weighted Nifty 500, 12.0%.
  - About 8 points of M's return is simply equal weights in small and mid caps in 2021–2026,
    one of the strongest runs Indian small and mid caps have had.
  - Momentum's selection within that universe added about 20 points a year before costs
    (40.7% against 20.3%).
- **Trading at the signal's own close:** M buys at the same month-end close its signal is
  computed from. Bought at the next session's close instead, it made **35.2%** after costs
  rather than 38.0%. A real portfolio, deciding after the close, gives up about 3 points.

**M's year by year (B5, before costs):**

| Year | M | Real index (A) |
|---|---|---|
| 2021 | +91% | +52% |
| 2022 | +10% | −6% |
| 2023 | +57% | +41% |
| 2024 | +53% | +20% |
| 2025 | +6% | −5% |
| 2026 to September | +33% | −5% |

## What it means for the app

- **The 38% isn't a data error.** It is an equal-weighted, monthly, small-and-mid-cap
  momentum strategy measured over a strong period for exactly that.
  - Its edge over its own universe is real in this data: about 20 points a year before
    costs.
  - About 3 points a year of it is trading at the signal's own close.
  - About 8 points is the universe's own run.
- **Forward, the period's tailwind can't be counted on.** Nor can costs stay at 0.195% each
  way for small caps at a larger size. The Plan's "13–16% a year, with falls of 30%"
  remains the sober expectation (`docs/PLAN.md`).
- **The index is a different product:** large caps, cap-weighted, slower. That is why it
  made 14%. An index fund on it doesn't capture what M does.
- **Nothing in the app changes** from this study.

## Corrections during the study

The first run of `gap.py` had two bugs in the study's own code, not in M or the engine. Both
were fixed before the results above, and B0 to B4 were unchanged by the fixes.
- **Missing scores in the sort.** Its stock selection sorted with missing scores in the sort
  key, and a NaN there scrambles the whole order. M's universe has missing scores (recent
  listings), so B5 picked the wrong stocks and gave 25.1% instead of M's 40.7%. The
  protocol's own check caught it: B6 must equal M's 38.0%. Stocks without a score are now
  left out before sorting.
- **Stocks without a share count.** The universe-first check stopped on stocks without a
  share count, which can't have a market-cap weight. They are now left out of the
  market-cap-weighted steps, as the protocol's B0 already does.

## Limits

- **One period:** 69 months, in which momentum and small caps both did well. The development
  period (2016–2020) can't be compared, as the index's month-end values in NSE's daily files
  start in 2020.
- **The rebuild's approximations** are listed above. NSE's exact buffer rule wasn't
  available.
- **Price returns throughout:** no dividends, on either side.

## Files

- `PROTOCOL.md`: written and committed before the run.
- `fetch_index.py`: NSE's month-end index closes.
- `gap.py`: the rebuild and the steps. Its output is `gap.txt`.
- `extra.py`: the two checks beyond the protocol. Its output is `extra.txt`.
- **Inputs:** `research/promoter/build_all.py`'s price panel, `research/momentum_upgrades`'
  universe and scores, and NSE's PR archive of 30 October 2026 (for issue sizes).
