# Results days: tested

Skilled day traders earn the most around results announcements (Barber, Lee, Liu & Odean),
and Indian studies report a drift after results, mostly after bad news. This tests both on
the app's data. The rules were fixed before any result was seen
([research/results/PROTOCOL.md](../research/results/PROTOCOL.md)), chosen on early years (`CHOICE.md`) and tested once on
later ones.

**Results dates:**
- **Source:** NSE's daily board-meeting files, 2012 to October 2026: 99,858 meetings called
  to approve results, for 3,935 companies.
- **Reschedules:** dates announced within 20 days of each other count as one meeting, dated
  by the latest notice.
- **Spot checks:** TCS 10 October 2024, Reliance 14 October 2024, HDFC Bank 19 October 2024,
  Infosys 11 January 2024.
- **Release time:** results come out during or after the meeting, at a time these files
  don't give. So the session after the meeting is the first one on which they are surely
  public.

## R1, the session after results, intraday: no edge

**Data:** Upstox 5-minute candles, 197 Nifty 200 stocks, 2022 to October 2026.

**The trade:** at 09:45, take the move from the previous close to the 09:40 candle. Follow
it or fade it from the 09:50 open to the 15:15 open, when it is at least 1% or 2%. Results
are after Upstox charges and slippage on ₹1 lakh.

| | 2022–2024 (chosen on) | 2025–Oct 2026 |
|---|---|---|
| Fade moves of 1% or more (the best of four) | −0.05% a trade, t −0.7 | **−0.29%, t −4.4** |
| Follow moves of 1% or more | −0.19%, t −2.5 | not run |
| Same rules on other days (control) | −0.03% to −0.22% | −0.14% (fade, 1%) |

Early moves on results days are not reliably continued or reversed by the close. Neither
direction pays for intraday costs.

## R2, the weeks after results: close, but not passed

**Universe:** the survivorship-free daily panel's 250 most-traded stocks on the session
after the meeting.

**The reaction:** the close-to-close return over the session before the meeting to the
session after, less the universe's average.

**The trade:** buy at the next open and hold 5, 21 or 63 sessions, after delivery costs
(about 0.39% a round trip). Results are measured against the universe over the same days.

| Buy after a reaction of +5% or more, hold 21 sessions (the best of nine) | 2013–2020 (chosen on) | 2021–Oct 2026 |
|---|---|---|
| Events | 938 | 521 |
| Return over the universe, after costs, an event | +1.02% | **+1.11%** |
| t (monthly averages) | 2.24 | **1.52**: short of the 2.0 required |
| Years positive | 6 of 8 | 5 of 6 |

The drift held at about the same size in both periods: buying after a strong positive
results reaction beat the universe by about 1% over the next month. In the later period it
was too noisy to clear the bar set in advance, so it doesn't count as passed.

**Bad results keep falling** (reported, not a candidate, before costs). After a reaction of
−5% or worse, the stock lagged the universe:

- **2013–2020:** −1.04% over the next 5 sessions and −1.58% over 63.
- **2021–2026:** −0.88% over 21 sessions (t −2.8), in 5 of 6 years.

This matches the Indian evidence that bad news drifts for weeks. Delivery can't be shorted,
so it is a reason to avoid or sell, not a trade.

## What it means

- Trading the results day itself, intraday, doesn't pay.
- After results, the direction of the price reaction tends to persist for weeks, both ways.
  On the upside it is about +1% a month after costs, consistent but not yet statistically
  firm. On the downside it is more reliable.
- Both uses are now tracked forward on paper (below).

## Tracked on paper

The **Results days (paper)** page and the daily job's step **results reactions (paper)**
(`igs.results_drift`) follow both sides from here. Nothing places an order or skips a
real trade.

**Reactions:**
- **The release:** each company's first NSE financial-results filing for a period
  (`filing_ref`), mapped to the company by the NSE symbol valid on the filing date. A
  filing for a period that ended more than 120 days earlier is not counted.
- **The reaction:** the close-to-close return from the last session before the filing day
  to the first session after it (results filed during market hours fall inside it too),
  less the average of the same liquid stocks: 50-session average traded value of ₹50 crore
  or more, and traded on that session after. Only liquid companies count.
- **When:** each daily job records the reactions whose session after has traded, looking
  back 60 days the first time. Each reaction is recorded once (`results_reaction`), with
  the time it was recorded.

**After +5% or better: a paper buy** (`results_trade`):
- Entry at the open of the next session, held 21 sessions, exiting at that session's open.
  Until then it is marked to the latest close.
- Measured against the same liquid stocks over the same days, less delivery costs (about
  0.39% a round trip on ₹1 lakh, as for the momentum portfolios).
- A reaction recorded after that open (on the first run, or a filing loaded late) is kept
  as **missed** and not counted: entering late would not be the rule tested.
- A Telegram note gives each new buy. The page shows each buy and, once some have closed,
  their average against the market, the share that beat it, and the t-statistic. As for
  the intraday calls, a t of about 2 needs a few hundred trades: the backtest had about
  100 a year.

**After −5% or worse: a flag** for the 21 sessions after the reaction, from the time it was
recorded:
- **Momentum:** a third paper portfolio, **Rule, skipping bad results**, holds the rule's
  best ten without the flagged stocks; the next-ranked stock takes a flagged one's place
  ([MOMENTUM.md](MOMENTUM.md)). It starts at the first monthly rebalance after deploying.
- **AI calls:** a call made while its stock was flagged carries a note on the call's
  Telegram message and the AI calls page compares those buy calls with the other buy calls
  at each horizon. The AI itself is not told about the flag, so the comparison stays
  clean.
- A Telegram note gives each new flag, and the page lists the stocks flagged now.

The backtest's figures came from board-meeting dates and the 250 most-traded stocks; the
paper record uses filing times and a turnover floor, so it is close to, not the same as,
what was tested.
