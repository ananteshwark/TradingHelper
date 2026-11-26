# OrderBlock's method (order-block.com), tested

The owner asked to use order-block.com's concept to identify winning stocks.
- **Process:** the protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed
  (bd3f0bc) before anything was run. The choices (`CHOICE.md`, d2b3bfa) were committed
  before either confirmation period was run, once.
- **Verdict:** nothing passed, so nothing was built into the app.

## What order-block.com does

It is a paid board for Indian F&O stocks on 5-minute candles. It publishes no record of
results.
- **Market side:** read how many F&O stocks are up against down, and trade the dominant
  side.
- **Board:** rank stocks by "clean, one-directional strength" (the move against the
  stock's last 50 sessions). The top 3 a side are the picks, at most 2 a sector, with no
  stock that has fallen unusually far.
- **Order block:** the last opposite candle before a forceful move that broke structure.
- **Trade:** buy the first return to it, wrong below its far edge, with the targets at the
  next untaken highs. Buys only above VWAP, R:R from 1:1.5.

Its live score also uses open interest, which can't be replayed (no intraday history), so
the board here ranks on price alone.

## Data

- **5-minute:** Upstox candles for today's 197 Nifty 200 stocks, March 2022 – 8 October
  2026. Each day, only the ones in that day's F&O list count, about 129 of the market's
  ~196 F&O stocks.
- **Daily:** NSE bhavcopies 2013–2026, adjusted for splits and bonuses, and the 250 most
  traded stocks each day.
- **Costs:** Upstox intraday charges and 0.02% slippage; delivery charges on Rs 1 lakh
  (about 0.39% a round trip).

## Results

### Intraday: the board and the 5-minute order block lose after charges

Development is March 2022 – 2024; confirmation is 2025 – October 2026. Each cell gives the
net result a trade, then the t of daily P&L.

| Variant | Development | Confirmation |
|---|---|---|
| A1 the board's 6 picks at 09:50, held to 15:15 | −0.140% (t −5.67) | −0.126% (t −4.12) |
| A2 the same, dominant side only | −0.099% (t −1.84) | −0.117% (t −1.90) |
| B1 the first return to the order block | −0.101% (t −6.47) | −0.085% (t −4.08) |
| B2 the same, dominant side only | −0.069% (t −2.43) | −0.076% (t −2.04) |

- **The board doesn't find the day's winners.**
  - Its picks ended the day in their side's top fifth 23–25% of the time, against 20% by
    chance.
  - After 09:50 they did no better than the average F&O stock: −0.02% (t −0.9) and −0.00%
    (t −0.1).
  - The strength it ranks on is already in the price by 09:50.
- **Waiting for the zone doesn't create an edge.**
  - Zone trades were about zero before charges: −0.006% and +0.011% a trade.
  - The stop below the block was hit in 59–61% of them, and the target in about 29%.
  - At about 0.1% a trade, charges turn zero into a steady loss.
- **About the site's "zone against chase" example:** on the days a zone trade happened,
  simply holding from 09:50 made about +0.5%. That comparison uses hindsight, since those
  are days the stock went on breaking higher.

### Daily order blocks: C1 came close, but didn't confirm

C0 buys the next open after a daily break of structure. C1 buys the first return to that
break's order block within 20 sessions. Both are out after a close below the block, else
63 sessions later. Results are against the equal-weighted universe, net of costs.

| | Development 2013–2019 | Confirmation 2020 – July 2026 |
|---|---|---|
| C0 buy the break | −0.28% a trade (monthly t −0.78) | −0.35% (t −1.61) |
| C1 buy the first return | **+0.58%** (t 2.37, passes ≥ 2.2) | **+0.50%** (t 1.74, **fails** ≥ 2.0) |

C1 is the one idea from the site with a consistent sign. Why it was not built:
- **Too weak to confirm:** in the confirmation period, t 1.74 is under the bar. Over both
  periods together it is 2.92, but that pools in the period that chose it.
- **Fading:** in the confirmation period, 2020–2024 were each +0.3% to +1.1% a trade,
  2025 was −0.16% and 2026 +0.09%.
- **A few large winners:**
  - 77% of trades are stopped quickly, at −2.9% each on average to the universe.
  - The 23% held for 63 sessions make +11.9%.
  - Without the best 50 trades out of about 4,100, the confirmation mean is −0.4%.
- **Not a short list:** about 59 trades are open at any time, so it is a broad signal,
  not a way to name a few winners.

## Against the app's goals

- **Trading (after charges):** the method loses, in both periods.
- **Investing:** buying the first pullback after a daily break did better than buying the
  break itself, but not reliably enough to act on.

The Rankings order (momentum) remains the app's best tested stock picker
(`research/ai_tuning`, `docs/MOMENTUM.md`).

## Files

- `intraday.py` and `analyze.py`: tests A and B. Output in `intraday_dev.txt` and
  `intraday_holdout.txt`.
- `daily.py`: test C. Output in `daily_dev.txt` and `daily_holdout.txt`.
