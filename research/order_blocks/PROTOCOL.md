# OrderBlock's method (order-block.com): protocol

Written on 18 October 2026, before any variant below was run. Its SHA-256 is in
`PROTOCOL.sha256`. Choices are made on the development periods and written in `CHOICE.md`
before the confirmation periods are run, once.

## Question

The owner asked to use the concept of order-block.com to identify winning stocks. Does it
find stocks that go on to beat the market after charges?

## What order-block.com does

Taken from its home page and blog on 18 October 2026: "What is an order block?", "How to
read an Order Block popup", "The OrderBlock daily playbook" and "Let the zone come to
you". It is a paid board for Indian F&O stocks, intraday on 5-minute candles. It
publishes no record of results.

1. **Read the market** from about 09:20: how many F&O stocks are up against down. "160
   against 50 is a one-sided day. 105 against 105 is not." Trade the dominant side.
2. **The board** ranks every F&O stock each minute by "clean, one-directional strength":
   the move against the stock's last 50 sessions. "Choppy stocks score zero"; a stock that
   spikes and reverses doesn't qualify. The top 3 a side are the picks, at most 2 from one
   sector. A stock with an unusually large fall is "event risk" and gets no levels.
3. **The order block** is "the last opposite candle before a forceful move that broke
   structure": for a buy, the last red candle before a rally that closes above an earlier
   swing high ("a peak with lower highs on both sides") or the first 15 minutes' range.
   The move must "leave a gap" (a three-candle fair value gap) or close beyond the block
   by at least an average candle. Only finished 5-minute candles count. The block is
   broken when a 5-minute candle closes beyond its far edge.
4. **The trade:** wait for the first return to the zone, "the zone is where you act";
   the setup is wrong below S1, just beyond the block's far side; targets are the
   "liquidity" levels ahead (untaken swing highs, the day's high). The R:R uses the second
   level; 1:1.5 is given as a floor some use. Buys above VWAP, sells below. Avoid 09:15–
   09:20 and new trades after 15:00. Risk 1% of capital a trade.

Its live score also uses open interest, which can't be replayed here (no intraday OI
history), so the board below ranks on price alone.

## Data

- **5-minute:** Upstox candles for the 197 stocks of `research/ml/universe_nifty200.json`
  (today's Nifty 200, so survivorship favours buying), 3 January 2022 – 8 October 2026.
  Each day, only stocks with stock futures in that day's NSE F&O bhavcopy count (the F&O
  list as it was). Sectors: NSE's Nifty 200 industry labels. Candle k: 0 = 09:15 … 74 =
  15:25.
- **Daily:** NSE bhavcopies 2013–2026, adjusted for splits and bonuses as in
  `research/ml`, and that study's point-in-time universe: the 250 most-traded stocks each
  day.

## Intraday definitions

- **Scale:** σ50 = the stock's mean absolute close-to-close return over its previous 50
  sessions in the candle files (needs 50; trading starts once they exist). A stock without
  the previous session's candles is skipped that day.
- **At 09:50** (after the 09:45 candle, k = 6, has closed):
  - move = the 09:45 close against the previous session's close;
  - strength = move / σ50;
  - cleanliness ER = |c6 − o0| / Σ|cᵢ − cᵢ₋₁| over k = 0…6, with c₋₁ = o0;
  - **choppy** when ER < 0.3, or when the move since the open (c6 − o0) has the other sign
    from the move: score 0, not ranked;
  - score = strength × ER;
  - **event risk:** move ≤ −3 × σ50; not picked.
- **Picks:** the 3 highest scores (bullish) and the 3 lowest (bearish), at most 2 a sector.
- **Breadth:** stocks with move > 0 against move < 0. A side is **dominant** with at least
  60% of them.

## Test A: the board's picks, entered at once

Each pick bought (bullish) or sold short (bearish) at the 09:50 open (k = 7), closed at the
15:15 open (k = 72). No stop.

- **A1:** both sides, every day.
- **A2:** only the dominant side's 3 picks; nothing on a balanced day.

## Test B: the first return to the order block

Same picks as A at 09:50. For a bullish pick (bearish mirrored):

- **Swing high:** a candle whose high is above the highs of the 2 candles on each side,
  known once the 2 later candles have closed.
- **Break:** candle j closes above the break level for the first time. The break level is
  the latest swing high known before j; with none yet, the high of the first 15 minutes
  (k = 0–2), and only from k = 3.
- **Block:** the last red candle (close < open) before j, at most 12 candles before it.
  None, no block.
- **Force:** between the block and j there is a fair value gap (a candle whose low is
  above the high two candles earlier), or j closes above the block's high by at least the
  average candle range (h − l) of the previous session. Otherwise no block.
- **Zone:** Z = the block's high, S1 = its low.
- **Broken:** a later candle closes below S1. A newer block replaces an older one.
- **Entry:** from the 09:50 candle (k = 7) to the 14:55 candle (k = 68), the first time a
  candle's low reaches the current block's zone after the break, a buy at Z.
  - **Filled** only if the low is at least 0.02% below Z, at Z (or at the candle's open if
    it opens below Z and above S1).
  - **Skipped** if the candle opens below S1, or Z is below the day's VWAP up to the
    previous candle, or the R:R below 1.5. Each block is used once; one trade a stock a
    day.
- **Targets:** R1 = the highest high from the block to the entry. R2 = the lowest of the
  day's earlier swing highs and the previous session's high that lie above R1; with none,
  R2 = R1.
- **R:R** = (R2 − Z) / (Z − S1).
- **Exit**, in order: the target R2 (a limit; a candle reaching it fills there); a candle
  closing below S1, out at the next open; otherwise the 15:15 open. The entry candle can
  only stop out at its close.

Variants:

- **B1:** A1's picks.
- **B2:** A2's picks.

## Intraday costs

Upstox intraday charges on Rs 1 lakh a trade (`igs.intraday.costs`), and 0.02% slippage on
market orders (A's entries, all stop and time exits). None on limit entries and targets.

## Test C: daily order blocks, for investing

In the daily universe, with daily candles and the same definitions:

- **Swing high:** 2 sessions on each side.
- **Break:** the first close above the latest swing high known.
- **Block:** the last red session within 10 sessions before the break.
- **Force:** a fair value gap, or the break closes above the block's high by at least the
  14-session average range.
- **Zone:** Z = the block's high, S1 = its low.

Variants:

- **C0, chase:** buy at the next open after the break.
- **C1, the zone:** a buy at Z on the first session within 20 after the break whose low
  reaches 0.02% below Z (at the open if it opens below Z and above S1). Skipped if it opens
  below S1, or a close below S1 comes first.

Both: out at the next open after a close below S1, otherwise at the open 63 sessions after
entry. One open trade a stock at a time.

- **Result:** the trade's return less the equal-weighted universe's open-to-open return
  over the same days, less round-trip delivery costs on Rs 1 lakh at the entry price
  (`igs.momentum.cost_pct`, about 0.39%). The stock must be in the universe on the break
  day.
- **The t-statistic** is over months: the mean excess of trades entered each month.

## Periods

- **Intraday:** development March 2022 – December 2024; confirmation January 2025 –
  8 October 2026. That confirmation period was used by earlier intraday studies, but not
  with these rules.
- **Daily:** development 2013–2019; confirmation 2020 – entries up to July 2026.

## Pass rules

**Intraday:** daily net P&L (Rs, the day's trades on Rs 1 lakh each).

- **Candidate:** the variant of A1, A2, B1, B2 with the highest development t.
- **Passes** with development t ≥ 2.5 (four variants tried), then confirmation mean > 0
  with t ≥ 2.0.

**Daily:**

- **Candidate:** the better of C0 and C1 by development t.
- **Passes** with development t ≥ 2.2, then confirmation mean > 0 with t ≥ 2.0.

If nothing passes, nothing is built into the app. A passing variant goes into the app as a
paper tracker first, with its forward record, not as orders.

## Also reported (descriptive, not tests)

- **Hit rate:** how often the picks end the day (15:15) in the top fifth of that day's F&O
  stocks by their own side's return.
- **Zone against chase:** B's trades against the same stocks entered at 09:50.
- **Before costs:** gross results, to show what charges take.
