# Choices, made on the development periods only

Written before any confirmation period was run.

## Data checks, before any strategy result

- **Settlement price.** The protocol's option-implied settlement price (strike + call
  settle − put settle) failed its check against the Nifty futures' final settlement on
  monthly expiries: median error 21 points, 95th percentile 791. The expiring options'
  settle prices are not their final intrinsic values. It was replaced with the official
  Nifty 50 closing value (NSE's ind_close_all files), which is the final settlement price
  by definition and matched the futures' final settlement exactly on all 175 monthly
  expiries. Where a day's file is missing, the futures' final settlement or the UDiFF
  underlying value is used. Three expiries listed under a holiday date have no price and
  are skipped.
- **Futures prices.** Over the F1 holdings, the adjusted futures return and the stock's
  open-to-open adjusted return differ by a median of 0.8 percentage points; 11 of 653
  differ by more than 5. The large moves checked (CG Power +100% in February–March 2019,
  Syndicate Bank +77% in May 2014) are real.
- **Wings:** the iron condors' capital at risk is the average width of the two wings
  between the traded strikes nearest the targets, close to w × the futures price.

## Choices

- **F1, shorting stock futures after a bad results reaction: failed on development; not
  run on confirmation.** 653 events (2013–2020) with stock futures. Net an event: hedged
  −0.44% (t −0.13), naked −0.68%. Before costs, hedged −0.24%: in these large F&O stocks,
  the shares did not keep lagging the Nifty after a bad reaction.
- **F2, long-short momentum with stock futures: goes to confirmation** (no variants). On
  development, +0.94% a month net (t 0.81). The long leg made +0.80% and the short leg
  +0.14%; the worst month was −32.6%.
- **F3a, monthly Nifty iron condors: failed on development; not run on confirmation.**
  Both variants lost money:
  - (3%, 2%): −11.1% of capital a month, t −2.42;
  - (5%, 3%): −7.1%, t −2.15.
- **F3b, weekly Nifty iron condors: (2.5%, 1.5%) chosen**, with the higher t: +0.78% of
  capital a week, t 0.43, against (1.5%, 1%) at +0.79%, t 0.29.

Confirmation runs: `fno.py ... f2 holdout` and `fno.py ... f3b holdout 0.025 0.015`.
