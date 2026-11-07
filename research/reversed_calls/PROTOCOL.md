# Reversed calls: protocol

Written on 26 October 2026, before any reversed result was computed. Its SHA-256 is in
`PROTOCOL.sha256`.

## Question

The owner's view: most traders now use AI-driven algorithms, so they get the same calls
the app makes, and the volume that follows makes the calls fail. If so, the opposite trade
should do better: a short sell where the app says buy, a buy where it says sell.

**What reversal can and can't do.** A reversed trade has the original's move before costs
with the sign flipped, and pays costs again:

- reversed net = −(original's move before costs) − costs;
- so reversal beats the original only when the original **loses before costs**;
- it makes money only when the original loses before costs by more than the costs.

## The calls tested

**Computed trade by trade:** the two intraday models whose calls can be approved for real
Upstox orders. Each uses the candidate its study chose, unchanged:

| | Rule | Entry, exit | Slippage a side |
|---|---|---|---|
| **The 09:45 ML calls** (research/ml, Study B) | LightGBM, up to three buys and three short sells a day with a predicted move of at least 0.15% | the 09:45 candle's open, the 15:15 candle's open | 0.02%, as in its holdout |
| **The evening picks** (research/evening, Study D) | LightGBM, five seeds averaged, the same three and three | the 09:15 open, the 15:15 open | 0.05%, as in its holdout |

For each, the same picks are reversed: each buy becomes a short sell and each short sell
a buy, with the same entry and exit candles. Slippage goes against the reversed side, and
Upstox intraday charges on ₹1 lakh are computed for it (`igs.intraday.costs`).

**From published results:** where the trades weren't kept, reversed net = −(published move
before costs) − (published round-trip cost):
- the six rule-based intraday strategies (`docs/MOMENTUM.md`, "Intraday strategies tested
  first");
- the Rankings order's top 20 (`research/momentum_upgrades`).

  A month-long reversal can't be a cash-market short in India: short sales must be closed
  the same day, so it would take stock futures. Short futures earn roughly the futures'
  premium, about 7% a year. So the bound used here is −(the move before costs) + 7% a
  year, before any futures costs.

The AI's buy/hold/sell calls have no history outside the app's own database, so they are
not tested here.

## Data

- **Candles:** Upstox's public 5-minute candles for the 197 Nifty 200 stocks of
  `research/ml/universe_nifty200.json`, December 2021 to 24 October 2026, downloaded again
  with `research/seq/fetch.py` (the originals were lost with a cloud container).
- **Pipelines:** features and walk-forward predictions rebuilt with each study's own
  scripts (`research/ml/intraday_features.py`, `intraday_model.py predict`;
  `research/evening/features.py`, `model.py predict`).
- **Check:** the rebuilt original trades must match each study's published holdout figures
  before the reversal is read.

## Periods

Each model's own periods, as its study used them:

| | Development | Holdout |
|---|---|---|
| 09:45 calls | 2023–2024 | 2025 – 8 Oct 2026 |
| Evening picks | 2023–2024 | 2025 – 8 Oct 2026 |

After each holdout, 9–24 October 2026 is reported separately: about ten sessions, too few
to judge. Each year is reported too, to see whether the originals weakened lately, as
crowding would predict.

## Pass rule

A reversed model passes if its mean daily net P&L after costs (₹1 lakh a trade) is:
1. above 0 in development; and
2. above 0 with t ≥ 2.5 in the holdout, with a profit factor of at least 1.1.

The bar is above the studies' 2.0, since the reversal was asked for after the originals'
results were seen.

**What follows:**
- A reversed model that passes is tracked forward on paper first, like the originals.
  Nothing is traded with real money from this study.
- Nothing passing: nothing changes.

The rule-based strategies and the top 20 pass only if their reversed bound is above 0.

## Also reported

For each model and period:
- trades, mean move before and after costs, profit factor, ₹ a day and its t, original
  against reversed;
- the same for buys and short sells separately;
- the same by year, and for 9–24 October.
