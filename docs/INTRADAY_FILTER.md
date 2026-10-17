# Which intraday calls work, tested

The owner asked what separates the intraday calls that reach their target from those that
hit their stop, and for an ML engine that gives only the calls that pay. The study was
fixed and fingerprinted before any outcome (`research/meta_intraday/PROTOCOL.md`).

## How it was tested

- **Calls:** the app's current rules (`intraday-v4`) were replayed by the app's own code
  on Upstox 5-minute candles for 202 Nifty 200 stocks, from February 2022 to October 2026.
  That gave 86,023 calls, the first of each stock and day. Each was resolved as the paper
  record does: the limit at the call's price, then stop or target, else a time exit at
  15:15. The result is in R and rupees after charges on ₹1 lakh.
- **The difference:** 21 things known at the moment of each call were compared between
  calls that reached the target first and those that hit the stop first: the volume jump,
  momentum, distance from VWAP, the day's move, the gap, the stop's width, the Nifty's
  move, where the candle closed, the time of day, yesterday and the last five days.
- **The ML filter:** a ridge regression and gradient-boosted trees (LightGBM) learned from
  earlier calls to predict each new call's R after charges, refitted every quarter. A call
  was taken only when its prediction was at least 0, 0.10 or 0.25 R. One of these six was
  chosen on 2023–2024 and tested once on January 2025 to October 2026.

## What it found

**The calls lose (2022–2024, 49,346 filled):**
- 26% reached the target first, 55% hit the stop first, and 19% ran to 15:15.
- They won 38% of the time, after charges.
- They averaged −0.12 R a call, about −₹64 on ₹1 lakh.

**Winners and losers look the same when the call is made.** No input differed by more than
a twentieth of a standard deviation between calls that reached the target and calls that
were stopped. Every fifth of every input lost money, with win rates of 36–39%. The edges
were small:
- **Lost less:** wider stops and wider-range days (−0.06 R against −0.17 R), mostly because
  charges are a smaller share of a wider stop.
- **Lost more:** calls whose candle closed at its very top (−0.17 R), and calls after 10:05.
- **Nothing turned a group positive.**

**The ML filter doesn't find a profitable subset:**

| | Calls | Win rate | R after charges | ₹ a day | t |
|---|---|---|---|---|---|
| All calls, 2023–2024 | 38,190 | 38% | −0.12 | −5,201 | −6.3 |
| Chosen: ridge ≥ 0.10, 2023–2024 | 319 | 49% | +0.16 | +348 | 1.4 |
| All calls, 2025–2026 | 35,273 | 37% | −0.15 | −6,357 | −7.2 |
| Ridge ≥ 0.10, 2025–2026 (holdout) | 108 | 44% | +0.03 | +64 | 0.2 |

- **Not passed:** the holdout made close to nothing. 2026 lost, and it took too few calls
  to count (108, against a minimum of 150).
- **The trees** lost money at every threshold, even on the years they were chosen on.
- **What the filter does:** almost all of its effect is skipping calls (99.7% of them).
  Not trading these calls at all comes out about the same.

## What this means

1. **A stop-out can't be told from a winner at the moment of the call** with the
   information the app has. These rules' calls are close to coin flips. Their payoff of
   2 R at the target against 1 R at the stop doesn't cover a 26% target rate and charges.
2. **No call list is built on the filter,** as the protocol says. The rule calls stay as
   they are, and keep losing in this test. Automatic orders on them should stay off.
3. **The intraday ML model that passed its own holdout is a different one**
   ([INTRADAY_ML.md](INTRADAY_ML.md)). It picks stocks at 09:45 from the morning's
   pattern and the market, rather than filtering these breakout calls. Its record on the
   **ML intraday** page is where calls that pay may come from, if its forward record holds
   up. At the owner's request the rule calls are now switched off (Settings, Intraday
   trading) and the ML calls can be approved for real orders.
