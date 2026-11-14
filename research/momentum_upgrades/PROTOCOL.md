# Momentum upgrades from the published evidence: protocol

Written on 22 October 2026, before any part of it was run. Its SHA-256 is in
`PROTOCOL.sha256`. Which variants go to the confirmation period is decided on the
development period and written in `CHOICE.md` first; the confirmation period is run once.

## Question

The owner asked for a review of the published evidence on investing and trading, and
whether the app's results can be improved. The review ("Published evidence to improve
returns", kept outside the repository) found that the best-supported untested ideas fix
the one working signal, momentum, rather than add a new one. They are tested here against
the Rankings order as it is (momentum, top 20), after costs **and after Indian tax**:

- **R, residual momentum:** rank on the part of each stock's past returns the market
  doesn't explain (Blitz, Huij & Martens 2011; Hanauer & Windmüller 2023: Sharpe 1.65
  against 0.57 across 48 countries, with far smaller falls).
- **R0, residual momentum without the volatility scaling:** Indian momentum pays most in
  stocks with high stock-specific volatility (Ansari & Khan 2012), so dividing by it could
  hurt here; the review asked for both side by side.
- **F, frog-in-the-pan:** among the top 40, prefer momentum built from many small moves
  over a few jumps (Da, Gurun & Warachka 2014; Goyal, Jegadeesh & Subrahmanyam 2025:
  stronger in emerging markets, held out of sample).
- **E, the market's reaction to results:** among the top 40, prefer the stocks the market
  rewarded most around their latest results (Brandt et al. 2008).
- **T, tax-lot-aware management of the top 20:** no monthly trims, hold a slipped winner
  that is about to turn long-term, and harvest losses and the yearly long-term exemption
  (Israel & Moskowitz 2011). Its generic cousin, a rank buffer, already did worse after
  tax here (`docs/PLAN.md`), so T keeps a slipped stock only near the 12-month line.

## Data

- `research/ml_rating`'s point-in-time data: each month end's 500 most-traded stocks
  (delisted ones included), the momentum percentile the Rankings order uses (6- and
  12-month return over volatility), and the return to the next month end (to the last
  close when a stock stopped trading).
- `research/promoter`'s adjusted daily closes of every NSE stock.
- `research/ml_rating`'s quarterly results with their first broadcast times.

## Strategies, each month end

**M, the Rankings order:** the top 20 by the momentum percentile, equal weights.

**R:**
- Each stock's monthly returns over the 36 months to the month end (at least 24 of them)
  are regressed on the universe's equal-weighted monthly return.
- The score is the sum of the residuals over the 11 months before the latest one (the
  latest month skipped, as in the papers) divided by their standard deviation over the
  same 11 months.
- Top 20 by that score, equal weights. A stock without 24 months is not ranked.

**R0:** as R, but the score is the sum of those residuals, not divided.

**F:**
- Among the top 40 by the momentum percentile: the 20 with the lowest information
  discreteness, ID = sign(P) × (share of down days − share of up days).
- P is the return, and the days are the sessions, from 12 months to 1 month before the
  month end. At least 150 sessions; those with fewer rank after the rest, in momentum
  order.
- Equal weights.

**E:**
- Among the top 40 by the momentum percentile: the 20 with the highest reaction to their
  latest results.
- **Day 0** is the first session whose close follows the results broadcast.
- **The reaction** is the stock's return less the equal-weighted return of the month's
  universe, from the close two sessions before day 0 to the close of the session after
  it, results broadcast in the 100 days to the month end with that close by the month end.
- Stocks without one rank after those with one, in momentum order. Equal weights.

**T:** the top 20, managed for tax:
1. **No trims or top-ups:** a holding still in the top 20 keeps its weight, except that
   one above 10% of the book is cut back to 10%. Sale proceeds go equally into the top 20
   names not held.
2. **The 12-month line:** a holding that leaves the top 20 is kept, at its weight, if it
   still ranks in the top 60 and one of its lots shows a gain of at least 20% and turns
   long-term within the next 62 days. Otherwise it is sold.
3. **Losses:** a lot with a loss that would turn long-term within the next 35 days is
   sold, and bought back at the same close if the stock is still held.
4. **The exemption:** at the March month end, if the year's net long-term gains are below
   Rs 1.25 lakh, lots with long-term gains are sold and bought back, largest gain first,
   until they reach it.

All start in cash with Rs 10 lakh. Every rupee traded at a month end pays 0.195% (half of
the 0.39% round trip), buy-backs included. A month's return is Σ w × the return to the
next month end. A holding that leaves the month's universe is sold at that month end.

## Tax

Every strategy runs as lots: each purchase is a lot (date, cost), and sales take the
oldest lots first. A lot sold more than 12 months after it was bought is long-term. Each
financial year (April to March), today's rules (Budget 2024) for every year:
- short-term gains at 20%, long-term gains at 12.5% above Rs 1.25 lakh;
- short-term losses offset short-term gains, then long-term gains; long-term losses offset
  only long-term gains;
- losses left over are carried forward for 8 years and used oldest first, before the
  exemption;
- no cess or surcharge, as in `docs/PLAN.md`.

The year's tax is paid at the next April month end by selling every holding pro rata,
those sales' gains counting in the new year.

**After-tax wealth** at a month end: the portfolio's value less the tax that would be due
if everything were sold at that close (the year's realised gains plus every lot's gain,
less 0.195% sale costs).
- **The after-tax monthly return** is its change.
- **The after-tax return a year** compounds it from Rs 10 lakh to the period's end, as if
  the portfolio were sold then.

## Periods

- **Development:** months held January 2016 – December 2020.
- **Confirmation:** January 2021 – September 2026, starting again in cash.

Momentum was chosen as the Rankings order on these years, so M is favoured in both.

## Pass rule

Each of R, R0, F, E and T against M, on the paired difference of after-tax monthly
returns:
1. it goes to confirmation if its development mean difference is above 0;
2. it passes if its confirmation mean difference is above 0 with t ≥ 2.5 (five variants
   tried, so above the usual 2.0), and its after-tax return a year in confirmation is
   above M's.

A passing variant goes into the app as a paper track on the Momentum page first, not into
the Rankings order. Nothing passing: nothing changes.

## Also reported

For each strategy and period:
- return a year before tax (after costs) and after tax, volatility, Sharpe over 6% cash,
  worst fall;
- turnover a year, average holding period, the share of realised gains that were
  long-term, and tax paid;
- the paired t on returns before tax.

Also:
- for T, the steps one at a time (1; 1+2; 1+2+3; all four);
- **M in a fund wrapper**, as an illustration: M's returns less 0.3% a year of fund
  expenses, taxed once at 12.5% above Rs 1.25 lakh at the end, as a fund investor would
  be (a fund pays no tax inside, s.10(23D));
- the deflated Sharpe ratio of the best variant (Bailey & López de Prado), counting the
  six strategies.
