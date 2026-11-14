# Choices, made on the development period only (`dev.txt`)

Written before the confirmation period was run.

**Implementation note.** 12-1 month momentum needs 252 sessions of prices; the panel has
it from 2 January 2013, so the January 2013 rebalance (signal 31 December 2012) had no
universe. The first rebalance is February 2013 and development runs from 1 February 2013
to 31 December 2020, for every portfolio.

- **H1, volatility targeting: failed on development; not run on confirmation.** Both
  variants lowered the Sharpe ratio (L = 63: 0.38; L = 126: 0.44; M: 0.53; bootstrap
  shares 0.00 and 0.01) and left the maximum drawdown unchanged at −50%. That drawdown came
  in 2013, before the target had six estimates. Cutting exposure after volatile months
  missed the rebounds that followed them (2014, 2017, 2020).
- **H2, momentum with low volatility: variant (b) chosen.** (a) raised the Sharpe ratio to
  0.79 but its bootstrap share against M was 0.76, under 0.8. (b), momentum among the
  calmer half, met the criterion: Sharpe 0.82 against M's 0.53 (share 0.83) and LV's 0.43
  (share 0.90).
- **H3, market trend filter: failed on development; not run on confirmation.** (a), above
  the 200-session average, cut the drawdown from −50% to −45% but lowered the Sharpe ratio
  to 0.32. (b), 12-month return above zero, deepened the drawdown to −55% (Sharpe 0.26).
  Both were out of the market for some of the sharpest momentum gains.
- **H4, inverse-volatility weights, and H5, the top 30: go to confirmation** as fixed. On
  development, H4's Sharpe was 0.61 (share 0.89) and H5's 0.48 (share 0.40).

Confirmation run: `portfolios.py BUILD holdout H1=none H2=b H3=none`.
