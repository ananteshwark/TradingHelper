# Call quality review — 24 October 2026

Reviewed shared-branch commit `ae6896a` against the 22 October research dossier.
The objective is net money made and benchmark-relative performance, not a higher
win rate obtained by changing the definition of success.

## Evidence already in the repository

- `research/meta_intraday/holdout.txt`: the breakout-call ridge filter did not
  pass. Its held-out 108 calls averaged +0.031 R after charges, t 0.24, and 2026
  lost. It is not a justified new selection filter.
- `research/ml_rating/holdout.txt`: neither selected ML rating passed; the momentum
  baseline was stronger. `research/ai_tuning/results.txt` adopted no new blend.
  Published quality/value/ML ideas alone do not justify replacing that baseline.
- `research/aladdin_engine/holdout.txt`: the chosen risk-optimized momentum engine
  did not beat fixed momentum on its prespecified Sharpe test. Exposure control
  does not itself prove a better return strategy.
- `docs/INTRADAY_ML.md`: real approvals differ from paper in entry delay, stop and
  exit time. Its tested live-style variant has weaker evidence.

These are recorded repository studies, not independently reproduced production
results. Earlier empty-universe observations do not describe all the newer
research datasets now present in the repository.

## Corrections

1. **Reject mismatched history.** The downloader tolerates some stock failures;
   previously the decision could use a stock's old 22-row cache after the index
   window advanced. Require the same ordered, unique prior session dates, and
   report withheld stocks in the day record. This prevents missing sessions from
   becoming supposed one-day moves. The observed index window is the reference,
   not an independently verified exchange calendar or corporate-action audit.
2. **Reject late decisions.** Check the actual scan-completion clock before storing
   picks. Outside 09:45–09:55 IST, record an expired day and zero picks instead of
   issuing a late subset. On-time, valid calls retain the existing features, model,
   ranking and thresholds; existing calls are never rewritten.
3. **Daily forward evidence.** Compute the descriptive t-statistic on net rupees per
   complete decision day, including no-call cash days. Same-day calls are not
   independent evidence. Open/missing-data outcomes are not zero-return cash;
   withhold the statistic while any recorded day is unresolved. The page no longer
   calls a large t proof of skill, and handles undefined statistics without crashing.

These corrections have not demonstrated higher hit rate, expectancy or returns.
They change no model weights, buy-target minimum, production ranking, broker
permission, order size or live-trading switch.

## Next evidence

Audit forward results by long/short side, model version, independent decision date,
signal-to-entry delay, order amount, stop, fills, spread, charges and net P&L. Keep
missing outcomes visible. Daily aggregation still does not correct serial
dependence, repeated selection or paper/live differences.

Freeze a new candidate and compare with the unchanged strategy on new data. Count
failed trials; reuse of the already inspected 2025–2026 holdout is exploratory.
Do not promote a new return filter without sufficient independent, after-cost
evidence. This package promotes no new strategy.

Methodology references:
- Harvey, Liu & Zhu: https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.PDF
- Bailey & López de Prado: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- Frazzini, Israel & Moskowitz: https://www.aqr.com/insights/research/working-paper/trading-costs

Regression checks cover stale/short/duplicate histories, valid calendar gaps, late
and cross-date decisions, unchanged on-time selection, cash days, unresolved
outcomes and undefined statistics. Full database CI is required before shared-
branch publication under `AGENTS.md`; skipped database tests do not satisfy it.
Update the open PR description before release. Deployment and production-data
validation are separate checks.
