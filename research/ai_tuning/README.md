# Ranking weights set by a learning procedure, against fixed momentum

The owner asked for the ranking's parameters to be set by AI rather than by hand. Since
17 October 2026 the ranking scores momentum alone. This study let four procedures choose
the pillar weights, or the momentum window, from past data only, month by month or year
by year. Each was compared with leaving the weight on momentum.

The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed (084eec0) before
`tune.py` was run.

**Data:** the point-in-time NSE results and prices of `research/ml_rating`, 2013–2026, with
each month's 500 most-traded stocks.

**Why not a language model choosing the weights:** a model trained on text up to 2026 may
know which factors did well in these years, so weights it proposed for them couldn't be
told from hindsight. It can only be judged forward.

## The four procedures

| | What it does | Refitted |
|---|---|---|
| **FM**, factor momentum | Weights each pillar by its positive return over the last 12 months | monthly |
| **WFO**, walk-forward optimiser | Picks, from 126 weight mixes, the one with the best risk-adjusted record so far | each January |
| **LW**, learned linear weights | Regresses next month's return rank on the five pillars, using all months so far | each January |
| **MW**, momentum window | Picks among the app's momentum, 6-month, 12-month and 12-1 month return | each January |

## Results

**Trading:** the top 20, held a month, January 2016 to September 2026 (129 months). Returns
are over the universe, after costs (`results.txt`).

| | A month over the universe | Against fixed momentum | 2016–2020 | 2021–2026 |
|---|---|---|---|---|
| **Fixed momentum (the app now)** | **+1.14% (t 3.0)** | | | |
| WFO | +1.00% (t 2.8) | −0.14% (t −0.5) | −0.24% | −0.06% |
| MW | +0.94% (t 2.4) | −0.20% (t −1.2) | −0.31% | −0.11% |
| FM | +0.42% (t 1.1) | −0.72% (t −2.2) | −0.86% | −0.60% |
| LW | +0.29% (t 0.8) | −0.85% (t −1.8) | −0.53% | −1.13% |

**Investing** (the last 12 monthly cohorts of the top 50):
- fixed momentum made +0.45% a month (t 2.5);
- the best tuned procedure, MW, made 0.01% a month less over the same months.

**None passed, and none beat fixed momentum in either half.** The ranking stays on
momentum.

**What the procedures chose:**
- WFO kept 60% on momentum, plus growth and valuation, until 2020. From 2021 it put 60%
  on growth.
- LW spread the weight over momentum, low volatility and valuation.
- FM followed the pillars' recent returns. It was on momentum alone in only 5% of months.
- MW chose 6-month momentum in most years.

Each one moved weight away from momentum, towards whatever had done well in the
preceding years. Those pillars did not keep doing well.

This agrees with `research/ml_rating`, where a model learning from all the inputs also did
worse than momentum. On this data, letting the past choose the ranking's parameters cost
money against the simple choice.

## What momentum would have done with real money

Descriptive, not a test. These are the same data that chose momentum, so the figures
flatter it. The top 10 or 20 by the app's momentum score were held a month and replaced
each month-end, after costs (0.39% a round trip on the names replaced). January 2016 to
September 2026:

| | A year | After tax (rough) | Worst fall | Worst year | Losing years |
|---|---|---|---|---|---|
| Momentum, top 10 | 27.0% | 21.3% | −46% | −24% (2018) | 4 of 10 |
| Momentum, top 20 | 28.1% | 22.3% | −39% | −17% (2016, 2018) | 3 of 10 |
| The 500 most traded, equal weights | 13.1% | | −55% | −24% | 4 of 10 |
| Nifty 50 (price) | 10.2% | | −29% | +3% | 0 of 10 |

Year by year, top 20:

| 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 to Sep |
|---|---|---|---|---|---|---|---|---|---|---|
| −17% | +114% | −17% | 0% | +57% | +87% | +5% | +53% | +48% | +4% | +32% |

**Caveats:**
- **Dividends:** these are price returns, so dividends are left out.
- **Prices:** month-end closes, so the worst falls inside a month are deeper than shown.
- **Tax:** each year's gain is taxed at 20% as short-term; losses are not carried forward.
- **Concentration:** most of the gain came in 2017, 2020 and 2021. Four years of ten lost
  money, and in 2018 the top 10 lost almost a quarter.
- **The worst fall** ran from August 2018 to March 2020: about a year and a half below
  the peak, before 2020–2021 recovered it.
- **Another test:** the Nifty 200's 12-1 momentum, tested separately, lagged its basket in
  2013–2020 (`docs/MOMENTUM.md`). A different universe and definition gave a different
  answer for those years.

## Files

| Step | File |
|---|---|
| Protocol | `PROTOCOL.md`, `PROTOCOL.sha256` |
| Procedures, portfolios, record | `tune.py` (`results.txt`) |

`tune.py` imports `research/ml_rating/model.py` for the pillars and portfolios. It ran with
`uv run --no-project --with polars --with numpy python -I tune.py features.parquet
universe500.parquet NSE_INDEX_Nifty_50.json` on the scratch data, which is not kept in the
repository.
