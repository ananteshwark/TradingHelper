# Events at the start of big moves: protocol

Written and fingerprinted before any return or move statistic of this study was computed.

## Question

The owner's idea: look at what happened just before stocks began big rises or falls, and use
the same events to call stocks early in their moves. Two questions follow:
1. **Anatomy:** which events are common in the days before a big move starts, compared
   with how often they happen on any day?
2. **Prediction:** after such an event, bought (or sold) at the next open, does the stock
   beat the market by enough to trade, after costs?

An event can be present before most big moves and still be useless if it is also common
without one. Part 1 describes, Part 2 decides.

## Data

- **Prices:** NSE's daily bhavcopies, January 2012 to 8 October 2026, every EQ and BE
  stock, linked across renames and adjusted for bonuses and splits
  (`research/promoter/build_all.py`). Delivery quantities from NSE's daily delivery files.
- **Results days:** NSE's board-meeting files (`research/results`), meetings whose purpose
  mentions results.
- **Promoter trades:** NSE's insider-trading disclosures, November 2015 to May 2026
  (`research/promoter`).
- **Index inclusions:** NSE Indices' press releases (`research/index_changes`).
- **Corporate announcements:** NSE's corporate-announcement listing, January 2013 to
  October 2026: the symbol, the time NSE published it, and its category.
- **Bulk and block deals:** NSE's historical listing, 2013 to October 2026: the stock, the
  buyer or seller, buy or sell, quantity and price.
- **Futures open interest:** NSE's F&O bhavcopies (`research/fno`), stocks with futures.

## Universe

A stock-day is investable when, at that day's close, the stock traded on at least 200 of
the last 252 sessions, closed at Rs 20 or more, and its median daily traded value over 126
sessions was at least Rs 2 crore.

## Big moves (Part 1 only; they use the future and are never traded)

- **Abnormal return:** the stock's return less the equal-weighted average of investable
  stocks over the same days.
- **Up-move start** on day t: the abnormal return from t's close to the close 63 sessions
  later is at least +30%, and t's close is the lowest of the closes from t-10 to t+10.
- **Down-move start:** at most -25%, and t's close is the highest from t-10 to t+10.

## Events

Each is dated to the session t at whose close it was public. A timed disclosure counts on
the last session whose 09:15 open came before its publication time, so that trading starts
at the next open.

| Family | Event |
|---|---|
| Price and volume | traded value at least 3x its 50-session average; delivery quantity at least 3x its 50-session average; a close at a 250-session high; an abnormal day of +5% or more, or -5% or less; a gap of 3% or more up or down |
| Results | a results board meeting on t |
| Promoters | a promoter or promoter-group market purchase, or market sale, published |
| Index | inclusion in the Nifty 50 or Nifty Next 50 announced |
| Announcements | categories grouped by keyword rules (order wins, acquisitions and schemes, credit ratings, fund raising, buybacks, bonus and splits, senior resignations, investor meets, press releases, takeover-code disclosures, pledges, other) |
| Deals | a bulk or block deal: an institution buying or selling, anyone buying or selling |
| Futures | open interest up 25% or more over 5 sessions with the futures price up (long build-up), or with it down (short build-up) |

The keyword rules for announcement categories and for institutional names in deals are
written in `EVENTS.md` after listing the category and client names with their counts, and
before any return is computed.

## Part 1: anatomy (development years 2013-2020 only)

For up-move starts, down-move starts, and all investable stock-days, the share with each
event in the 5 sessions up to and including t, and in the 15 sessions before that. The
**lift** is the share among move starts divided by the share among all stock-days. Also
reported for each event: the share followed by an up-move start (or down-move start) within
10 sessions.

## Part 2: prediction

**Trade:** after an event on day t, buy at t+1's open and sell at the open H sessions
later (H = 21 or 63), or, for a sell call, the reverse. A stock's next event of the same
kind counts only after the previous holding ends.

**Measure:** abnormal return from the open at t+1 to the open at t+1+H, less costs:
`igs.momentum.cost_pct` at the entry price, plus slippage of 0.3% under Rs 10 crore a day
and 0.1% from Rs 10 to 50 crore. A sell call is measured on stocks with futures, as a short
futures position: minus the abnormal return, less 0.119%.

**Statistic:** calendar time. Each event's net return goes to its entry month; each month
with events counts once, at the mean of its events; t = mean / standard deviation x
sqrt(months).

**Candidates:**
- each event, in its natural direction (buy after the events that lift up-move starts,
  sell after those that lift down-move starts, as Part 1 shows), with H = 21 or 63, if it
  has at least 300 events in the development years;
- **a combined score:** a logistic regression of "abnormal return over the next 63
  sessions of +30% or more" (and, separately, -25% or less) on all event flags for the 5
  sessions up to t and 20 sessions up to t, plus the stock's 12-1 month return, distance
  from its 250-session high and log traded value. Fitted on 2013-2018. A stock-day is a
  call when its score is in the top 0.5% of 2019-2020's scores (the threshold is fixed
  there). H = 63.

**Choice:** on the development years 2013-2020 (the score's on 2019-2020, its validation
years), the buy candidate and the sell candidate with the highest calendar-time t of
net returns. If none has a positive mean, that side's confirmation is not run.

**Confirmation:** entries from 1 January 2021 to the last that completes its holding by
8 October 2026, run once for the two chosen. These years' prices have been used in other
tests here; the events haven't.

**Passing:** mean net return above zero with t at least 2, and positive in at least 4 of
the 6 years.

If a rule passes, it becomes a paper call list in the app, tracked forward before any real
money.
