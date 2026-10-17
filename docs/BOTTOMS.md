# Calling the bottom, tested

The owner wants to buy near the bottom, before the upward move. The earlier study
([MOVE_STARTS.md](MOVE_STARTS.md)) found that big rises start after sharp falls, but buying
sharp falls lost money because most kept falling. This study tested six rules that wait for
signs the fall has ended. The rules were fixed and fingerprinted before any result
(`research/bottoms/PROTOCOL.md`), chosen on 2013–2020, and run once on 2021–2026.

## The rules

Every rule needs a fall first: the stock's lowest close of the last six months is at least
30% below its one-year high. The data is daily prices, traded value and delivery for every
NSE stock trading at least ₹2 crore a day, 2013 to October 2026. It has no history of
quarterly results, so the rules use the chart only.

| Rule | Buys when |
|---|---|
| R1 Stage-2 breakout | the price crosses above its 150-day average, which has stopped falling, on 1.5× normal volume |
| R2 Off the low | the price is 25% above its six-month low, above a rising 50-day average |
| R3 Capitulation, then a higher low | after a crash day at a one-year low on 3× volume, the price holds above that low and rises 10% |
| R4 Accumulation at the lows | near the one-year low, delivery runs at twice its usual level and the price stops falling |
| R5 Tight base breakout | after 30 sessions in a 15% range, the price breaks out on 2× volume |
| R6 Rebound state | a long-term loser (bottom 30% over 12 months) becomes a short-term winner (top 30% over a month) |

Each was bought at the next open and held 3 or 6 months (63 or 126 sessions). Returns were
measured against the average investable stock, after costs.

## What it found

**2013–2020 (`research/bottoms/dev.txt`):**
- Five of the six lost money against the market after costs, at both holding periods.
  - The rebound state (R6) lost about 3% a trade, with t −4.9.
  - The tight-base breakout (R5) lost about 1%.
  - Capitulation then a higher low (R3) lost 12% a trade over six months.
- Only accumulation at the lows (R4) was positive: +2.2% a trade over three months on 131
  trades. Even there it reached only t 1.55, short of the bar of 2.

**2021–2026, run once for R4:** −2.9% a trade, t −2.36, positive in 2 of 6 years. **Not
passed.**

**Timing, 2013–2020:**

| Rule | Bought above the eventual low (median) | Fell another 10% first | Big rises caught |
|---|---|---|---|
| R1 | 25% | 56% | 2% |
| R2 | 27% | 59% | 0.5% |
| R3 | 24% | 57% | 0.1% |
| R4 | 14% | 46% | 0.7% |
| R5 | 30% | 52% | 10% |
| R6 | 27% | 58% | 13% |

The columns:
- **Bought above the eventual low:** how far the entry was above the lowest close in the
  three months either side.
- **Fell another 10% first:** the share of entries that fell another 10% within three
  months.
- **Big rises caught:** of the 2,373 big rises (+30% against the market in three months),
  the share where the rule bought within a month of the low, within 15% of it.

## What this means

1. **The bottom can't be called from the chart.** No rule beat the market after costs.
   They bought a quarter above the low on average, and half the time the stock fell another
   10% first. With the move-start study, that covers every liquid NSE stock from 2013 to
   2026.
2. **No bottom call list is built,** as the protocol says.
3. **The AI is told,** and calls "before the move" only on evidence from the business: results
   turning, margins recovering. It also expects a fallen stock may fall further.
4. **The business side is tested forward instead.** The data here has no results history,
   so two records in the app test it from now on:
   - **AI calls page, by stage:** how stocks went on against the Nifty 500 after the AI
     placed them "before the move" (and "near a top").
   - **Results days page:** positive quarterly results on stocks 30% or more below their
     one-year high, which is the business turning while the price is down. The results
     Telegram message says so when it applies.

   If either record shows those stocks beating the market over enough calls, that is the
   way to the bottom the chart didn't give.
