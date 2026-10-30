# Spike reversal: fade a 12% move in 30 minutes: protocol

Written on 28 October 2026, before any trade below was simulated. Its SHA-256 is in
`PROTOCOL.sha256`. The variant is chosen on the development period and written in
`CHOICE.md` before the confirmation period is run, once.

## Question

The owner's rule: "If any stock rises or falls more than 12% in 30 minutes, it is going to
recover by 4%, so buy or sell at the moment the candle becomes horizontal, with a target
profit of 4%."

Does fading such a move make money after Upstox's intraday charges and slippage?

What the owner left open, and how it is settled here:

- **"Horizontal":** a 5-minute candle with a small body. Three thresholds are tried.
- **The stop:** none was given. Three are tried, among them none.
- **The time exit:** 15:15, as in the app's other intraday rules.

## Already known before writing this

Only counts of the moves, not what followed them:

- In the 197 stocks of `research/ml/universe_nifty200.json` (today's Nifty 200), January
  2022 – October 2026: 89 stock-days with a 12% move inside 30 minutes, 12 of them from
  2025 on. That is too few to test, so the test is on all liquid NSE stocks.
- Market-wide: 18,070 candidate stock-days (below).

## Data

- **Candidate days:** NSE's daily bhavcopies, EQ series, 1 January 2022 – 8 October 2026
  (`candidates.py`):
  - the day's high at least 12% above its low;
  - the median traded value of the previous 20 sessions at least Rs 2 crore.
  - A move of 12% inside the session can't happen on any other day, so the first filter
    loses no event. The second uses only what was known before the day.
  - BE-series stocks are trade-for-trade, with no intraday trading, so they are left out.
  - Delisted and renamed stocks stay in: the bhavcopy is the list as it was.
- **5-minute candles:** Upstox's public historical candles for those days (`fetch.py`).
  - Candle times are their start: 09:15 … 15:25.
  - A day is used only if its candles run from 09:15 to at least 15:15. That drops
    Muhurat and other special sessions.
  - **Bad data:** a day whose 5-minute high exceeds the bhavcopy's high by more than 0.5%,
    or whose low is more than 0.5% below the bhavcopy's low, is dropped.
  - Candidate days with no candles from Upstox are counted and reported.

## Definitions

- **The move:** at the close of each candle t, from the 09:15 candle to the 14:55 one:
  - rise = close(t) / the lowest low of the candles starting from t − 25 minutes to t,
    less 1;
  - fall = close(t) / the highest high of those candles, less 1.
  - The window is by clock time and stays inside the day: never yesterday's close, and
    shorter in the first 25 minutes.
- **Trigger:** the first candle of the day with rise ≥ 12% (a **spike up**) or fall ≤ −12%
  (a **spike down**). One trigger a stock a day; its side is fixed.
- **Flat candle:** the first candle at or after the trigger, starting no later than 14:55,
  with:
  - |close − open| ≤ h × open;
  - high > low. A candle without a price range is a halt or a circuit lock, not a stall;
    the search goes on past it.
- **Entry:** at the open of the next candle, against the move: a short sell after a spike
  up, a buy after a spike down. No flat candle by 14:55: no trade.
- **Target:** 4% from the entry, as a limit order. A candle reaching it fills at the target,
  or at its open when it opens beyond it.
- **Stop:** a candle reaching it fills at the stop, or at its open when it opens beyond it.
  When one candle reaches both, the stop counts first.
- **Time exit:** the open of the 15:15 candle; for an illiquid stock without one, the
  open of the first candle after 15:15, else the last close.
- The entry candle can reach the target or the stop.

## Variants

Nine, from three flat thresholds and three stops:

- **Flat threshold h:** 0.25%, 0.5% or 1.0% of the open.
- **Stop:**
  - **S0:** none, out at 15:15;
  - **S4:** 4% from the entry (as far as the target);
  - **SX:** 0.5% beyond the move's extreme: for a short, the highest high from the start
    of the trigger's window to the flat candle, plus 0.5% of the entry; for a buy,
    mirrored.

## Costs

- Upstox intraday charges on Rs 1 lakh a trade (`igs.intraday.costs`; quantity =
  Rs 1 lakh / entry, rounded down).
- **Slippage: 0.15% a side** on the market entry, stops and the time exit, and none on
  the target, which is a limit order.
  - That is more than the 0.02–0.05% of the app's large-cap studies: these stocks have just
    moved 12% and many are small.
  - Also reported: 0.05% and 0.30%.
- Every signal is taken, Rs 1 lakh each.

## Periods

- **Development:** 1 January 2022 – 31 December 2024.
- **Confirmation:** 1 January 2025 – 8 October 2026, run once, with the chosen variant.

## Pass rule

The measure is the daily net P&L in rupees over every session of the period: the day's
trades added up, Rs 1 lakh each, and zero on a day without one.

- **Candidate:** the variant with the highest development t.
- **Passes** with:
  - development t ≥ 2.5 (nine variants tried);
  - then a confirmation mean > 0 with t ≥ 2.0;
  - and at least 100 confirmation trades.

If nothing passes, nothing is built into the app. A passing variant goes into the app as a
paper tracker first, with its forward record, not as orders.

## Also reported (descriptive, not tests)

- **The owner's claim itself:** after a spike, how often the price comes back 4% from the
  entry before 15:15, against how often it goes on another 4% the other way first.
- Long and short sides separately.
- The Nifty 200 subset.
- Busy days: the result with at most the first 5 signals a day.
- The same rule after 8% and 10% moves, which are more common. These are not tests and
  can't pass.
- **Before costs:** gross results, to show what charges take.
- Trades whose exit candle has no price range (a circuit lock), where the exit order might
  not have filled.

## Limits known in advance

- **Intraday restrictions aren't replayed:** brokers block intraday trading in stocks
  under NSE's ASM and GSM surveillance. Many small stocks that move 12% are among them.
  The history of those lists isn't here.
- **Price bands:** stocks outside F&O have fixed bands of 2–20%. A short on a stock near
  its upper band can't lose beyond it that day, but the order to close it may not fill if
  the stock locks there.
- **Spreads:** they can't be measured from candles, so slippage is assumed and varied.
