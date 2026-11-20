# Predictive-model research, October 2026

The owner asked for predictive models after rule-based calls kept losing. Two studies
were fixed in `PROTOCOL.md` before any model was fitted. Its SHA-256 is in
`PROTOCOL.sha256`: once at writing, once after Amendment 1, which changed how prices are
adjusted before any Study A result. For each study, one candidate was chosen on the
development years alone (`CHOICE_A.md`, `CHOICE_B.md`), then tested once on a holdout.

## Study B, intraday: passed; tracked forward on paper

Gradient-boosted trees on Upstox 5-minute candles predict each liquid Nifty 200 stock's move
from 09:45 to 15:15. Each day it takes up to three buys and three short sells whose
predicted move is at least 0.15%.

- **Holdout (2025 to October 2026):** +0.13% a trade after costs, t 2.4, profit factor
  1.19, positive in both years.
- **Caveats:** almost all of the profit came from the short sells. It came in a few months
  and was weaker in 2026. See `docs/INTRADAY_ML.md`.

Files:

| Purpose | Files |
|---|---|
| Data | `fetch_candles.py`, `universe_nifty200.json` |
| Features | `intraday_features.py` |
| Walk-forward and the eight candidates | `intraday_model.py` (`intraday_dev.txt`, `intraday_holdout.txt`) |
| Robustness, market share, what it picks | `intraday_robust.py`, `intraday_beta.py`, `intraday_explain.py` and their `.txt` |
| The app's model and its feature check | `export_model.py`, `check_ml_features.py` |

## Study A, swing: failed

Data: every NSE equity from the daily bhavcopies, 2012 to October 2026, so delisted and
fallen stocks are included. Each day the universe is the 250 most traded stocks.

- **Prices** are adjusted from NSE's corporate-action files (Amendment 1), plus unrecorded
  bonuses, splits and demergers inferred from overnight moves beyond −40% or +60%.
  `check_adjust.py` compares the result with Upstox's adjusted candles: of 566,146 stock-days,
  318 differ by more than 2%. Most of these are Upstox's own adjustments in 2012–2016.
- **Features:** 28 stock characteristics, ranked across stocks each day, plus six market
  ones.
- **Models:** ridge and LightGBM, retrained each January on all earlier data, predicting
  5- or 21-day returns against the universe.
- **Portfolios:** the top 10 or 20, weekly or monthly, with a 2N buffer. Each name traded
  pays delivery costs, about 0.39% a round trip.

| | Development 2016–2020, per month vs the universe | Holdout 2021–Oct 2026 |
|---|---|---|
| Chosen: LightGBM, weekly, top 20 | +0.83% (IR 0.88, t 2.0) | **+0.55% (IR 0.67, t 1.6)**: fails t ≥ 2; 5 of 6 years positive |
| Momentum baseline, weekly, top 20 | +0.62% (t 1.2) | +1.11% (t 2.1) |
| Momentum, monthly, top 10 (the app's paper rule) | +1.57% (t 2.3) | +0.68% (t 0.9) |

The model beat the universe on average, but not reliably enough after its costs. About
0.8% a month went on trading 47% of the names each week. Plain 12-1 momentum, which the
app already tracks on paper, did at least as well with almost no trading. The momentum rows
are baselines, not candidates. Picking one of them now, after seeing the holdout, would
need its own forward test.

Files:

| Purpose | Files |
|---|---|
| Data | `fetch_bhav.py`, `fetch_bc.py`, `parse_bhav.py` |
| Adjustment | `corp_actions.py`, `build_daily.py`, `check_adjust.py` |
| Models and portfolios | `daily_model.py` (`daily_dev.txt`, `daily_holdout.txt`) |

The scripts ran in a scratch environment with `uv run --with lightgbm --with scikit-learn
python -I`. The downloaded data is not kept in the repository.
