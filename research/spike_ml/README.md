# Spike recovery with machine learning

The owner asked:
- use machine learning to find by what percentage a stock has moved when it recovers the
  most, and how big that recovery is;
- the same logic and percentage would then be used for intraday trades.

The test covered every liquid NSE stock that moved 5–15% within 30 minutes, 2022 – 8
October 2026: 103,935 decision points on 240,767 stock-days of Upstox 5-minute candles.

The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed (2e279e5) before
any model was trained. The choice (`CHOICE.md`) was committed (563acff) before the test
period was run.

**Verdict:**
- **The recovery peaks after moves of about 10%:** a median of about 3% before 15:15, with
  about half reaching 3%. Bigger moves recover no more.
- **A model can rank spikes by how much they will recover,** but not well enough to trade
  after costs.
- **The trading rule built on it failed:** its 2024 validation fell short of the bar, and
  it made +0.06% a trade in 2025–2026. Nothing is built into the app.

## How far the price comes back

The maximum recovery: the largest move back against the spike from the next candle's open,
before 15:15, before costs.

| Move within 30 minutes | Median, 2022–2024 | Median, 2025–2026 | Mean, 2022–2024 | Mean, 2025–2026 | Reached 3%, 2022–2024 | Reached 3%, 2025–2026 | Minutes to it, 2022–2024 | Minutes to it, 2025–2026 |
|---|---|---|---|---|---|---|---|---|
| 5% | 2.4% | 2.3% | 2.9% | 2.8% | 39% | 37% | 55 | 65 |
| 6% | 2.6% | 2.5% | 3.2% | 3.1% | 44% | 42% | 50 | 60 |
| 7% | 2.9% | 2.7% | 3.5% | 3.3% | 48% | 45% | 50 | 55 |
| 8% | 3.1% | 2.9% | 3.8% | 3.6% | 51% | 48% | 48 | 50 |
| **10%** | **3.3%** | **3.1%** | 4.3% | 3.8% | **53%** | **52%** | 45 | 45 |
| 12% | 3.3% | 2.9% | 4.7% | 3.9% | 53% | 49% | 35 | 40 |
| 15% | 3.4% | 2.5% | 4.7% | 3.9% | 53% | 41% | 25 | 30 |

- **The recovery grows with the move up to about 10%.** In 2025–2026 it was smaller after
  12% and 15% moves.
- **Reaching 1% is common:** 74–82% of spikes, at any size.
- **The typical maximum is under 3.5%,** so a 3% target is met about half the time at
  best.
- **Bigger moves recover sooner:** in 25–45 minutes from 10% up, against about an hour
  after a 5% move.
- **These are the best prices reached, not trades.** A recovery of 3% often came after the
  price had first gone further against the trade.

## What the model could do

**The recovery model** (LightGBM, trained on the years before):
- **It ranked spikes:** the correlation between predicted and actual recovery was 0.42 in
  2024 and 0.21 in 2025–2026.
- **In 2025–2026:**
  - the tenth it rated highest recovered 4.1% on average (predicted 5.2%);
  - the tenth it rated lowest recovered 2.1%;
  - 53% of the top tenth reached 3%, against 23% of the bottom.
- **Its main inputs were mostly the day's context** rather than the spike's shape:
  - the previous session's return;
  - how many other stocks had spiked the same way that day;
  - the Nifty's last 30 minutes;
  - the move from the previous close (which stands in for the price band).

**The trading rule:** take the first point of a stock's day where the best predicted
after-cost result, across targets of 1–5%, beats a gate; use that target.

| | 2024 (validation) | 2025 – 8 Oct 2026 (test) |
|---|---|---|
| Gate | 0.5% (the best of five) | 0.5% |
| Trades | 4,918 | 3,985 |
| A trade, after costs | +0.46% | **+0.06%** |
| Rs a day (t) | +9,143 (t 2.21; the bar was 2.5) | +561 (t 0.24) |
| The best day / the worst day | 4 June 2024: +Rs 8.86 lakh on 615 trades | 7 April 2025: −Rs 9.71 lakh on 493 trades |

- **It didn't pass:** the validation t of 2.21 was below the 2.5 fixed in advance, and the
  test made almost nothing.
- **Fixed rules on the same points failed too:** in 2025–2026 every one lost, or made at
  most 0.16% a trade (12% moves, 5% target, t 0.45).
- **Slippage:** at 0.05% a side the rule made +0.23% a trade in 2025–2026 (t 0.96); at
  0.30% it lost 0.20%.

## What showed up along the way (not tested)

- **Buys after falls did better than short sales after rises,** in both periods:
  - 2024: +1.33% a trade on 1,271 buys, against +0.16% on 3,647 short sales;
  - 2025–2026: +0.60% on 867 buys (t 2.23), against −0.09% on 3,118 short sales.

  This was seen after the fact, so it isn't a result. It would need its own test, on data
  from now on.
- **Locked exits again:** 275 of the test's trades (7%) ended on a candle without a price
  range, losing 4.1% each.

## Forward on paper

At the owner's request, the model's buys after falls and short sales after rises are
followed forward on paper, apart, from the first session after deployment:
- `PAPER.md` (sha256 in `PAPER.sha256`) fixes the rule and how each record will be judged;
- [docs/SPIKES.md](../../docs/SPIKES.md#spike-picks) describes the alerts and the tab.

## Limits

- The same as `research/spike_sizes`:
  - brokers' intraday restrictions aren't replayed;
  - spreads are assumed;
  - price bands can lock an exit.
- **No past price bands:** the model couldn't use the paper track's band rule.
- **The test period was seen before:** 2025–2026 was the confirmation period of the two
  earlier studies of this idea, though no model here had been run on it.

## Files

- `prepare.py`: the spike-sizes days with volume, the previous close and the previous
  session's return.
- `events.py`: the decision points, their features and outcomes.
- `export_model.py`: the paper track's model, trained once on every point to date and
  exported to `config/models/spike_ml_v1.json`. It checks the app's features against
  `events.py`'s and the exported trees against LightGBM.
- `model.py`: the models, the gate and the reports.
  - Output in `validate.txt` and `test.txt`.
  - After the test, the recovery table's tenths were fixed to hold one row each; one row
    had fallen into an eleventh. Every other figure was unchanged when it was rerun.
