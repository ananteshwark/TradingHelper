# Spike sizes: which move within 30 minutes is best for a 3% gain: protocol

Written on 28 October 2026, before any trade below was simulated. Its SHA-256 is in
`PROTOCOL.sha256`. The variant is chosen on the development period and written in
`CHOICE.md` before the confirmation period is run, once.

## Question

The owner asked: among stocks that move more than 5%, which size of move is best for a 3%
gain?

Read here as: after a stock moves X% within 30 minutes, which X, entry and direction makes
the most from a 3% target after costs? The spike-reversal study
(`research/spike_reversal`) tested only X = 12%, against the move, with a 4% target.

## Already known before writing this

- The spike-reversal study's results for 12% moves:
  - before costs, trading against the move made 0.3–0.45% a trade;
  - after costs, about nothing;
  - its development and confirmation periods are the same as here.
- In today's Nifty 200, 2022–2026, there were 1,180 rises and 634 falls of 6% or more
  inside 30 minutes. The counts were looked at, not what followed them.

## Data

- **Candidate days:** NSE's daily bhavcopies, EQ series, 1 January 2022 – 8 October 2026
  (`candidates.py`):
  - the day's high at least 5% above its low (a 5% move inside the session can't happen on
    any other day);
  - the median traded value of the previous 20 sessions at least Rs 2 crore.
  - 279,490 stock-days.
- **5-minute candles:** Upstox's public historical candles for those days (`fetch.py`).
- **Kept as in the spike-reversal study:**
  - **Full sessions:** a day is used only if its candles run from 09:15 to at least 15:15.
  - **Bad data:** a day whose 5-minute high or low is more than 0.5% outside the bhavcopy's
    is dropped.
  - Days with no candles from Upstox are counted.

## Definitions

- **The move:** as in the spike-reversal study. At the close of each candle t, from the
  09:15 candle to the 14:55 one:
  - rise = close(t) / the lowest low of the candles starting from t − 25 minutes to t,
    less 1;
  - fall = close(t) / their highest high, less 1.
- **Trigger at threshold X:** the first candle of the day with rise ≥ X or fall ≤ −X
  (checked in that order). One trade a stock a day for each variant.
- **Entry**, at a candle's open, for which there are two choices:
  - **N, now:** the open of the candle after the trigger;
  - **F, flat:** the open of the candle after the first flat candle at or after the
    trigger. A flat candle has |close − open| ≤ 1% of the open and a high above its low,
    and starts by 14:55. No flat candle by then: no trade.
  - Either way, no trade if the entry candle starts at 15:15 or later.
- **Direction:**
  - **fade:** against the move (a short sale after a rise, a buy after a fall);
  - **follow:** with it (a buy after a rise, a short sale after a fall).
- **Target:** 3% from the filled entry, a limit order. A candle reaching it fills at the
  target, or at its open when it opens beyond it.
- **Stop:**
  - **S0:** none;
  - **S3:** 3% from the filled entry. A candle reaching it fills at the stop, or at its
    open when it opens beyond it.
  - When one candle reaches both, the stop counts first.
- **Time exit:** the open of the first candle at or after 15:15.

## Variants

56 variants:

- **X:** 5%, 6%, 7%, 8%, 10%, 12% or 15%;
- **entry:** N or F;
- **direction:** fade or follow;
- **stop:** S0 or S3.

## Costs

- Upstox intraday charges on Rs 1 lakh a trade (`igs.intraday.costs`).
- Slippage of 0.15% a side on the entry, stops and the time exit; none on the target.
- Also reported: 0.05% and 0.30% a side.
- Every signal is taken.

## Periods

- **Development:** 1 January 2022 – 31 December 2024.
- **Confirmation:** 1 January 2025 – 8 October 2026, run once, with the chosen variant.

## Pass rule

The measure is the daily net P&L in rupees over every session of the period, zero on a day
without a trade.

- **Candidate:** the variant with the highest development t.
- **It passes** with all of:
  - a development t of at least 3.0 (56 variants tried);
  - a confirmation mean above zero with t ≥ 2.0;
  - at least 100 confirmation trades.

If nothing passes, nothing is built into the app.

## Also reported (descriptive, not tests)

- **The direct answer to the question:** for each X and entry, before costs, how often the
  price went 3% against the move first, 3% with it first, or neither by 15:15.
- The 56 variants' results in each period, before and after costs.
- For the candidate:
  - rises and falls separately;
  - stocks trading Rs 2–10 crore a day against Rs 10 crore or more;
  - today's Nifty 200;
  - the first 5 signals a day;
  - the busiest days;
  - trades that ended on a candle without a price range (a lock).

## Limits known in advance

- As in the spike-reversal study:
  - brokers' intraday restrictions (ASM and GSM) aren't replayed;
  - spreads can't be measured from candles;
  - the price bands of stocks outside F&O can lock an exit.
- **Thresholds of 12% and 15%** overlap the spike-reversal study's trades and periods.
