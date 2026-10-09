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
- A practical use, if the owner wants it:
  - don't buy a stock (AI calls, momentum picks) within a month of a results reaction of
    −5% or worse;
  - track "buy after a strong positive reaction" forward on paper, as with the other models.
