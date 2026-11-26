# Protocol: ranking weights set by a learning procedure, against fixed momentum

Written on 17 October 2026 before any candidate below was computed. Its sha256 is in
`PROTOCOL.sha256` and the file is committed before `tune.py` is run.

## Question

The owner asked for the ranking's parameters to be set by AI rather than fixed by hand.
Since 17 October 2026 the ranking scores momentum alone (config/scoring.yaml). Can a
procedure that learns the pillar weights, or the momentum window, from data available at
the time beat that fixed choice after costs?

## What has already been seen

`research/ml_rating` reported each pillar's own top-20 return for 2016–2020 and
2021–2026 (`pillars.txt`): only momentum was positive in both. The momentum-only ranking
was chosen from that. So every period here has been looked at before. The candidates
below are standard methods from the literature, fixed before running them, and each one
uses only months before the one it ranks. Even so, the idea of testing pillar timing at
all comes after those results. A pass here would be evidence, not proof, and would still
need its forward record.

## Why not a language model (Claude) choosing the weights

A model trained on text up to 2026 may know which Indian factors did well in 2016–2026.
Weights it proposed for those years could not be told apart from hindsight. It can be
judged only forward, so it is not a candidate here.

## Data

From `research/ml_rating` (scratch data, not in the repository):
- `features.parquet`: month-end inputs and forward returns from January 2012 to
  September 2026.
- `universe500.parquet`: each month's 500 most-traded stocks.

Pillar scores are built as in `model.prepare`:
- each factor's percentile rank within the month, in its better direction;
- each pillar is the mean over its factors; ownership is absent.

Months from January 2013 onwards are used (momentum coverage is 41% in 2012 and 99% or
more after).

**Score for a weight vector w:** the weighted mean of the stock's non-null pillars,
renormalised over the weights present. A stock none of whose weighted pillars has a value
is not ranked.

## Comparators

- **M, fixed momentum:** the momentum pillar alone (`p_momentum`, the mean percentile of
  6- and 12-month return over volatility). This is what the app uses now.
- **U, the universe:** the equal-weighted 500.

## Information set

A portfolio formed at month-end `t` may use only rows whose month-end is before `t`.
Their next-month returns are known by `t`.

## Candidates (four, fixed)

1. **FM, factor momentum, refitted monthly** (Gupta and Kelly 2019; Ehsani and
   Linnainmaa 2022).
   - At each `t`, each of the five pillars' trailing return is the mean, over the last 12
     month-ends before `t`, of its own top 20's gross return minus the universe's.
   - Weights are proportional to the positive trailing returns.
   - If none is positive, momentum alone.
2. **WFO, walk-forward optimiser, refitted each January.**
   - The 126 weight vectors on a 0.2 grid over the five pillars are candidates.
   - Pick the one with the highest t-statistic of net top-20 excess return over all
     month-ends from January 2013 to before the refit. At least 36 months are needed;
     January 2016 is the first refit.
   - On a tie, the vector with more weight on momentum wins.
   - The weights hold for the year's twelve portfolios.
3. **LW, learned linear weights, refitted each January.**
   - Regress, by ordinary least squares with an intercept, each stock-month's next-month
     return percentile on its five pillar percentiles (missing pillars set to 0.5).
   - Use all rows from January 2013 to before the refit.
   - Weights are the slopes floored at zero, normalised to sum to 1. If all are zero,
     momentum alone.
4. **MW, momentum window, refitted each January.**
   - Four definitions are candidates: the app's (mean percentile of 6- and 12-month return
     over volatility), 6-month alone, 12-month alone, and 12-1 month return.
   - Pick the one with the highest t-statistic of net top-20 excess return, measured as
     in WFO.

A January refit is done at the December month-end, for the portfolio held in January.

## Portfolios (as in research/ml_rating)

- **Trading, primary:** the top 20, held a month. Costs are 0.39% a round trip times the
  share of names replaced.
- **Investing, reported:** the last 12 monthly cohorts of the top 50. Each cohort keeps
  the weights of its month.

The test months are January 2016 to September 2026, reported in total and as 2016–2020
and 2021–2026.

## Pass rules (trading)

A candidate passes if all hold over January 2016 to September 2026:
- **(a)** its monthly net return minus M's averages above zero, with a paired t of 2.24
  or more (5% one-sided, Bonferroni for four candidates);
- **(b)** that difference averages above zero in 2016–2020 and in 2021–2026;
- **(c)** its net excess over the universe has t ≥ 2.

If more than one passes, the one with the highest t in (a) is adopted. The app would then
set its weights by that procedure, and the Momentum page would track it forward. If none
passes, the ranking stays on fixed momentum.

## Descriptive, not part of the pass

- What M's top 10 and top 20 did in absolute terms after costs, January 2016 to
  September 2026, against the Nifty 50 and the universe: a year, worst fall, worst year,
  losing years.
- Each candidate's weights over time.
