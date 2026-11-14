# Calling the bottom: protocol

Written and fingerprinted before any return or timing statistic of this study was computed.

## Question

The owner wants to buy near the bottom, before the upward move. The earlier study
(`research/move_starts`, `docs/MOVE_STARTS.md`) found that big rises start after sharp falls,
but buying after sharp falls lost money: most kept falling. This study tests rules that wait
for evidence the fall has ended, from price, volume and delivery alone (the data has no
history of quarterly results):

1. **Trading them:** bought at the next open after the rule fires, does the stock beat the
   market by enough, after costs?
2. **Timing:** how close to the bottom does each rule buy, how often does the stock fall
   further first, and what share of the big rises does it catch early?

## Data

NSE's daily bhavcopies, January 2012 to 8 October 2026, every EQ and BE stock, linked across
renames and adjusted for bonuses and splits (`research/promoter/build_all.py`: adjusted open
and close, traded value). Delivery quantities from NSE's daily delivery files, as value
(delivery quantity x the day's close, which splits don't change).

## Universe

As in `research/move_starts`: a stock-day is investable when, at that day's close, the stock
traded on at least 200 of the last 252 sessions, closed at Rs 20 or more, and its median
daily traded value over 126 sessions was at least Rs 2 crore. Windows below count the
stock's own trading sessions.

## Rules

All on adjusted closes (C), traded value (V) and delivery value (D), known at the close of
session t. **The fall:** the lowest close of the last 120 sessions is at most 70% of the
highest close of the last 250 sessions (a fall of 30% or more within about six months).
Every rule needs the fall on t.

| Rule | Fires on t when |
|---|---|
| **R1 Stage-2 breakout** | C crosses above its 150-session average (C(t-1) at or below it), the average is no lower than 20 sessions earlier, and V(t) is at least 1.5x its average of the previous 50 sessions |
| **R2 Off the low** | C(t) is 25% or more above the lowest close of the last 120 sessions, for the first time in 63 sessions, and C is above its 50-session average, which is higher than 10 sessions earlier |
| **R3 Capitulation, then a higher low** | Between t-40 and t-5 there was a capitulation day c: a 250-session low close, V(c) at least 3x its previous 50-session average, and the day's return at least 5% below the equal-weighted average of investable stocks. Since c, no close has been below C(c); C(t) is at least 10% above C(c) and the highest close since c. Once per capitulation day |
| **R4 Accumulation at the lows** | C(t) within 15% of the 250-session low; the average D of the last 20 sessions at least 2x its average over the 120 sessions before them; C(t) at or above C(t-20) |
| **R5 Tight base breakout** | The closes of the previous 30 sessions span at most 15% (highest / lowest), C(t) is above all of them, and V(t) is at least 2x its previous 50-session average |
| **R6 Rebound state** | The stock's 12-1 month return (C(t-21) / C(t-252) - 1) is in the bottom 30% of investable stocks on t and its 1-month return (C(t) / C(t-21) - 1) in the top 30% (Goulding, Harvey and Mazzoleni's "rebound" state), and it wasn't on t-1 |

## Part 1: trading

**Trade:** after a rule fires on t, buy at t+1's open and sell at the open H sessions later
(H = 63 or 126). A stock's next signal from the same rule counts only after the previous
holding ends.

**Measure:** abnormal return from the open at t+1 to the open at t+1+H: the stock's return
less the equal-weighted average of the stocks investable on t over the same days, less
costs: `igs.momentum.cost_pct` at the entry price, plus slippage of 0.3% under Rs 10 crore a
day of median traded value and 0.1% from Rs 10 to 50 crore.

**Statistic:** calendar time. Each trade's net return goes to its entry month; each month
with trades counts once, at the mean of its trades; t = mean / standard deviation x
sqrt(months).

## Part 2: timing (development years only; described, never chosen on)

For each rule's signals:
- **Distance from the bottom:** the entry (the open at t+1) against the lowest close from
  t-63 to t+63; the median, and the share within 10% of it.
- **Fell further first:** the share whose lowest close in the 63 sessions after entry is
  10% or more below the entry.
- **Big rises:** the share followed by an abnormal return of +30% or more over 126 sessions.
- **Big rises caught:** of the up-move starts of `research/move_starts` (an abnormal return
  of +30% or more over the next 63 sessions from a close that is the lowest of the 21
  around it), the share where the rule fired within 21 sessions after the start, with the
  entry within 15% of the start's close.

## Choice, confirmation, passing

**Choice:** on the development years 2013-2020, the rule and H with the highest
calendar-time t of net returns. If none has a positive mean, no confirmation is run.

**Confirmation:** entries from 1 January 2021 to the last that completes its holding by
8 October 2026, run once for the chosen rule and H. These years' prices have been used in
other tests here; these rules haven't.

**Passing:** mean net return above zero with t at least 2, and positive in at least 4 of
the 6 years.

If a rule passes, it becomes a paper call list in the app, tracked forward before any real
money, and the AI's calls are told of it. If none passes, no bottom call list is built.
