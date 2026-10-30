# The choice, made on 2016–2020 only

Written before the confirmation period was run (`dev.txt`).

## The data, rebuilt

The scratch data of `research/ml_rating` and `research/promoter` was lost with a cloud
container before anything here was run. It was rebuilt from NSE's archives with the
repository's own scripts (`research/ml/fetch_bhav.py`, `fetch_bc.py`, `parse_bhav.py`,
`corp_actions.py`, `research/promoter/build_all.py`, `research/ml_rating/fetch_ranges.py`
and `fetch_if.py`) and `inputs.py`, which redoes the universe, the momentum percentile and
the results broadcasts as ml_rating defined them.

It matches the original:
- **Prices:** 6,507,889 rows, the original's count.
- **Universe:** 86,500 rows from June 2012, 1,435 stocks against 1,440.
- **Momentum:** missing for the same share of stocks each year.
- **M, development:** 18.4% a year, volatility 30.3%, Sharpe 0.52, worst fall −39%;
  the Aladdin study's M, on the original data, gave 18.2%, 30.3%, 0.51 and −39%.

One difference from ml_rating's text: its universe didn't apply the "200 of the past 252
sessions" rule its protocol states. Only without it does the rebuild match (`inputs.py`).

## Development results

After costs; "after tax" is the after-tax wealth if everything were sold at the end.

| | Before tax | After tax | Sharpe | Worst fall | After tax against M (a year) | t |
|---|---|---|---|---|---|---|
| M, the Rankings order | 18.4% | 15.0% | 0.52 | −39% | | |
| R, residual momentum | 15.3% | 12.1% | 0.44 | −34% | −3.1 points | −0.59 |
| R0, unscaled | 5.6% | 4.3% | 0.16 | −57% | −8.4 points | −1.34 |
| **F, frog-in-the-pan** | **25.1%** | **20.7%** | **0.76** | **−32%** | **+3.9 points** | **0.86** |
| E, results reaction | 17.8% | 14.3% | 0.51 | −39% | −0.8 points | −0.21 |
| **T, tax-lot-aware** | **19.9%** | **17.1%** | **0.56** | **−39%** | **+2.0 points** | **1.02** |

**Going to confirmation (development mean difference above 0): F and T.**

**Stopping here: R, R0 and E.**
- **Residual momentum** lost to plain momentum. It did cut the worst fall (−34% against
  −39%) and won 2018 (+4% against −14%), as the papers suggest, but gave up more in the
  strong years.
- **Without the scaling (R0)** it was far worse. That fits the caution in the review:
  Indian momentum pays in volatile stocks, and the unscaled residual leans hardest on
  them.
- **The results reaction** changed little. A reaction was found for 34 of the top 40 a
  month, on average; the rest had no results in the 100 days, or fell in NSE's 2018 gaps.

## What each survivor does

**F** held the 20 of the top 40 whose rise came on the most days.
- It was ahead in three of the five years, and behind M in 2017 (+73% against +94%) and,
  narrowly, 2019 (−1.4% against −0.2%).
- It had a lower volatility (26% against 30%) and a smaller worst fall (−32% against
  −39%).
- The deflated Sharpe ratio, counting the six strategies, was 0.81, short of the 0.95
  that would make it convincing on these years alone.

**T's gain came from its first two steps.** Steps 3 and 4 never had anything to act on:
- at no March month end did T hold a lot older than a year (the oldest was 271 days);
- top-20 momentum holdings rarely last twelve months.

| Step | After tax against M (a year) | t |
|---|---|---|
| 1, no trims | +1.0 points | 0.59 |
| 1 and 2, holding near the 12-month line (5 times) | +2.0 points | 1.02 |
| All four | +2.0 points | 1.02 |

**What tax costs here:**
- M realised none of its gains long-term; a sold rupee was held 2.4 months on average.
- Tax took 3.4 points a year (18.4% to 15.0%).
- **M in a fund wrapper** (0.3% a year of expenses, 12.5% once at the end) would have
  kept 16.5%, more than M held directly.

## The confirmation test

January 2021 to September 2026, starting again in cash, run once. F and T each pass if:
- the mean of its after-tax monthly difference from M is above 0, with t of at least 2.5;
- and its after-tax return a year is above M's.
