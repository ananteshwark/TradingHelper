# Choices, made on the development period only (`dev.txt`)

Written before the confirmation period was run.

- **S2, hedged momentum: variant (a), the beta hedge, chosen.** Over cash:
  - (a) h = beta: +0.20% a month, t 0.51;
  - (b) h = 1: +0.08%, t 0.19.

  The average hedge beta was 1.09. Its correlation with the Nifty was 0.02, against 0.80
  for the unhedged momentum portfolio.
- **S3, index trend following: failed on development; not run on confirmation.** Both
  variants lost money over cash:
  - (a) the 12-month return: −0.67% a month, t −1.12, including −7.3% a month in 2020,
    when it was short into the rebound;
  - (b) the 200-day average: −0.07%, t −0.12.
- **S1, momentum through futures against shares** (descriptive): futures with 6% cash made
  1.29% a month, shares 1.41%. Lower trading costs did not make up for monthly rollovers,
  carry and the dividends a futures holder forgoes.

Confirmation to run once: `futures.py ... holdout S2=a S3=none`.
