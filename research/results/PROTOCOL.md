# Results-day study: protocol

Written 2026-10-09T11:47:06Z, before any result of this study was seen.

Barber, Lee, Liu & Odean found that skilled day traders earn the most around results
announcements, and Indian studies report a drift after results, mostly after bad news.
This tests both on the app's data.

**Results dates.** NSE's daily board-meeting files (Bm*.txt in the PR archive, 2012 to
October 2026). A meeting whose purpose mentions results counts. When a company announces
dates within 20 days of each other, the one in the latest file stands (a reschedule). The
board-meeting day is B. Results come out during or after the meeting, at a time these files
don't give, so B+1 (the next session) is the first session on which they are surely
public.

## R1: intraday on B+1 (Upstox 5-minute candles, 197 Nifty 200 stocks, 2022 to Oct 2026)

- **Signal at 09:45 on B+1:** the move from the previous close to the 09:40 candle's close
  (gap plus first 30 minutes).
- **Entry and exit:** a market order at the 09:50 open, exit at the 15:15 open.
- **Costs:** 0.02% slippage a side and Upstox charges on Rs 1 lakh.
- **Candidates:** follow the move or fade it, when it is at least 1% or at least 2%. That
  makes four.
- **Choice:** the highest t-statistic of the mean net return a trade in 2022–2024.
- **Pass (2025 to Oct 2026, once):** mean net return above zero with t >= 2, profit
  factor >= 1.1, at least 100 trades.
- **Control (reported):** the same rule on days that are not B+1, to show whether any effect
  belongs to results days.

## R2: drift after results (the survivorship-free daily panel, 2013 to Oct 2026)

- **Universe:** the 250 most-traded stocks on B+1.
- **Reaction:** the close-to-close return from B-1 to B+1, less the universe's average
  over the same days.
- **Trade:** buy at the open of B+2 when the reaction is at least the threshold, and hold
  H sessions. Delivery costs as in Study A, about 0.39% a round trip.
- **Result:** the holding's return less the universe's over the same days.
- **Candidates:** threshold +3%, +5% or +8%; H = 5, 21 or 63 sessions. That makes nine.
- **Choice:** the highest t-statistic of the mean net excess return an event in 2013–2020,
  with t computed on monthly averages so that overlapping events don't inflate it.
- **Pass (2021 to Oct 2026, once):** mean net excess above zero with t >= 2 on monthly
  averages; positive in at least 4 of the 6 years.
- **Reported, not a candidate:** the same for reactions of -3%, -5% and -8% or worse. A
  drift after bad news is something to avoid or sell, since delivery can't be shorted.
