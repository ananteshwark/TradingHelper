# Spike reversal: the development result and the choice

Written on 28 October 2026, after `spike.py dev` (output in `dev.txt`) and before the
confirmation period was run.

## Development, 2022–2024

- 18,070 candidate stock-days:
  - 1,357 had no candles from Upstox. Of the 213 ISINs involved, 157 have since been
    replaced by a new ISIN (after a split or the like) and 49 no longer trade;
  - 1,279 were dropped as not a full session (listing days, special sessions) or with a
    range off the bhavcopy's by more than 0.5%;
  - 15,434 used, 9,129 of them in development.
- **Every one of the nine variants loses after costs**, with t from −0.21 to −0.81. They
  make 0.24–0.31% a trade before costs; slippage (0.15% a side) and charges take 0.34%.
- **Candidate** (highest development t): a flat candle within 1.0%, no stop (S0). 746
  trades; −0.044% a trade; −Rs 43.5 a day; t −0.21; profit factor 0.97.
- **It does not pass:** the pass rule needs a development t of at least 2.5.
- **With 0.05% slippage**, every variant makes a little (+0.06% to +0.13% a trade), none
  with t above 0.9. With 0.30%, every variant loses 0.29–0.36% a trade.

## Things the development period showed

- **The owner's claim:** after a spike and a flat candle, the price came back 4% before
  15:15 in 31% of cases; it went another 4% the wrong way first in 24%; neither, 45%.
- **One day carried the period:** 4 June 2024, the election results, had 57 trades and
  made Rs 1.01 lakh. Without it, the candidate lost Rs 1.34 lakh over the period.
- **Locked exits:** 98 trades ended on a candle without a price range, losing 3.5% each.
  They are mostly short sales of stocks locked at their upper price band, where the order
  to buy them back might not have filled at all.
- **Today's Nifty 200:** 54 trades, +0.05% a trade, t 0.05.

## The choice

The candidate, a 1.0% flat candle with no stop, is run once on the confirmation period,
1 January 2025 – 8 October 2026, for the record. It has already failed the development
test, so the study does not pass whatever the confirmation shows, and nothing is built
into the app.
