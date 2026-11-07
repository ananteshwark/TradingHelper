# Two ideas from how the Indian market works, tested

Both ideas come from how Indian stocks are owned and traded rather than from price patterns,
and neither had been tested here before:
- **Promoters buying their own shares** on the open market, with their own money.
- **Stocks joining the Nifty 50 or Nifty Next 50**, which index funds must then buy on a date
  announced weeks ahead.

As before, the rules were written down and fingerprinted before any result was seen
(`research/promoter/PROTOCOL.md`, `research/index_changes/PROTOCOL.md`). Choices were made on
the early years, and the later years were run once. Costs are today's delivery charges
(about 0.4% a round trip), with more slippage for smaller stocks.

## Results

| Idea | Earlier years | 2021–2026 | Result |
|---|---|---|---|
| Promoter open-market purchases of ₹1 crore or more, held 63 sessions | +1.28% a trade over the market (t 2.0), 2015–2020 | +0.18% a trade, −0.54% a month in calendar time (t −0.6) | **failed** |
| Stocks joining the Nifty 50, bought after the announcement, sold at the close before the change | +0.98% a trade over the Nifty (t 0.95), 2011–2019 | −3.06% (t −1.3), 2020–2026 | **failed** |
| Stocks joining the Nifty Next 50, the same trade | +1.27% a trade (t 0.6) | +1.89% (t 1.75) | **not passed** |

## Promoters buying their own shares

**Data:** NSE's insider-trading disclosures from November 2015 to May 2026: 29,971
open-market purchases by promoters and promoter groups, in 1,327 companies. They were
grouped into 6,052 events in stocks trading at least ₹2 crore a day, each bought at the
open after NSE published the disclosure. Disclosures came a median of two days after the
purchase.

**Results:**
- In 2015–2020, purchases of ₹1 crore or more, held for three months, beat the market by
  1.28% a trade after costs. That came from a minority of big winners: the median trade
  lost 0.94%. Smaller purchases and one-month holds did worse.
- In 2021–2026 the same rule made 0.18% a trade. Counting each month once, it lost 0.54%
  a month, and only 45% of trades beat the market.

**Likely reasons it doesn't work here:**
- Indian promoters often buy to defend a falling price or to creep up their stake, not
  because they expect good news.
- The disclosure reaches the market days after the purchase.
- Many purchases are in small companies, where costs are higher.

## Stocks joining the Nifty indices

**Data:** NSE Indices' press releases on index changes from 2011 to 2026, 178 of them,
giving each change, the day it was announced and the day it took effect. One semi-annual
review, August 2021, is a scanned document without text and is missing. 93 inclusions in
2011–2019 and 91 in 2020–2026 were tested, each against the largest Nifty 50 ETF over the
same days.

**Nifty 50 joiners lost money after 2020:** −3.06% a trade on average, and only 19% beat
the Nifty.
- These are the largest companies, and which ones will join is predictable months ahead
  from their free-float market value.
- Traders now buy them before the announcement, so they tend to fall from it to the day
  the funds buy. Examples: Zomato and Jio Financial in 2025 (−10% and −6%), and Bharat
  Electronics in 2024 (−10%).

**Next 50 joiners gained, but not reliably:**
- +1.27% a trade in 2011–2019 and +1.89% in 2020–2026, over about 23 sessions.
- Neither reaches significance. One stock, Adani Green Energy (+68% in 2020), carries much
  of the later figure: without it, the average is +1.0% (t 1.4).
- By year, 2024–2026 were near zero.

**How the result was reached** (`research/index_changes/NOTES.md`):
1. The first confirmation run showed the Next 50 trade passing (t 2.03).
2. Checking that pass revealed that the parser had missed the Next 50 inclusions of
   three reviews in 2024–2025, and the script had dropped the August 2026 review.
3. With the data corrected, it falls short: the pass came from missing data.

## What this means

1. **Neither idea becomes a strategy or a paper portfolio here.**
2. **A promoter buying is not, on its own, a reason to buy.** In this market it was as
   likely to come before a further fall.
3. **Index inclusions are a crowded trade.** By the time a Nifty 50 change is announced,
   the price has usually moved. The Next 50 effect may be real but small, and it has
   faded in recent years.
4. **The Plan** ([PLAN.md](PLAN.md)) stays as it is: momentum, gold and cash.
