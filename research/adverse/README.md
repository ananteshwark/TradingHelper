# Adverse moves after the app's recommendations

**Result: the parameters behind adverse moves are clear and held out of sample. None of
the models trained on them earned more after costs than the recommendations as they are.**

**What drives adverse moves:**
- **Intraday trades:** stop-outs come from the day's own volatility, against a stop set by
  the past 14 days'.
- **Momentum months:** the bad ones come from the most volatile, most extended picks.

**Why the models don't help:** the same volatility makes the big winners. A veto that
skips the risky trades skips those winners too. An early exit cuts the losers sooner but
also cuts the trades that would have recovered.

The record of what happens after each recommendation now starts in the app
(docs/TRACKING.md). The models run there in shadow, changing nothing.

Pre-registered in `PROTOCOL.md` (pushed in 1bf8ded before any data was built). The
development choices are in `CHOICE.md` (94cb631), and the holdout ran once.

## Study A: trading, the 09:45 calls and the evening picks

**The trades:** each model's three best buys and short sells a day, as real orders take
them:
- entry at 09:50 (the 09:45 calls) or 09:20 (the evening picks);
- stop at half the 14-day ATR;
- out at 15:00.

Costs: Upstox charges on ₹1 lakh, and slippage of 0.02% (09:45 calls) or 0.05% (evening
picks) at each end.

| | Development (Jul 2023 – 2024) | Holdout (2025 – 23 Oct 2026) |
|---|---|---|
| 09:45 calls | +0.065% a trade, ₹275 a day, 25% stopped | **+0.079%**, ₹281 a day, 25% stopped |
| Evening picks | +0.020%, ₹80 a day, 26% stopped | **−0.062%**, **−₹205 a day**, 27% stopped |

**The evening picks lose money with the real order rule.** Their paper record (in at
09:15, out at 15:15, no stop) made +0.14% a trade (research/evening). With the 09:20
entry, the stop and the 15:00 exit that real orders use, they lost 0.06% a trade in
2025–2026. That rule had not been backtested before (docs/INTRADAY_EVENING.md said so).
**Evening real orders shouldn't be switched on as they are.**

### What goes with a stop-out

A stopped trade loses about 2% after costs. A trade that isn't stopped makes about +0.7%.
Stopped trades seldom went the right way first: their median best move was +0.1 ATR.

**At the decision.** Confirmed drivers: the same sign in both periods, |t| ≥ 2 in the
holdout. Stop rates by fifth of each parameter are in the holdout, lowest fifth to highest:

| Parameter | 09:45 calls | Evening picks |
|---|---|---|
| The first 30 minutes' range over the ATR (`range30_atr`) | 13% → 43% (t 8.5) | not an input |
| Volume in the first 30 minutes against usual (`rvol30`) | 12% → 40% (t 8.1) | |
| Volume in the signal day's last 30 minutes (`rvol_last30`) | | 21% → 34% (t 3.6) |
| The ATR itself, as % of the price (`atr_pct`) | 36% → 17% (t −5.8) | 30% → 20% (t −3.0) |
| Liquidity, 20-day traded value (`log_value20`) | 28% → 17% (t −3.4) | |

So the stop at half the 14-day ATR is too tight on a day that is moving more than usual:
a wide first half hour or heavy volume. It is also too tight in a stock whose ATR is
small.

**Direction doesn't matter:**
- **Against the Nifty's move (H1):** not confirmed (t 1.3 and 1.8 in the holdout).
- **The model's conviction (H4):** not confirmed (t 0.8 and −1.0).
- **Distance from VWAP (H5):** t 2.1 in development, 0.5 in the holdout; not confirmed.

**Heavy volume at the decision (H2)** goes with bigger favourable moves, and so bigger
winners: the top fifth's best move was 0.32 to 0.69 points larger, t 2.8 to 5.9. Its
adverse moves were larger only for the evening picks in the holdout.

**After the entry.** Fifteen minutes in, a trade that is down, below VWAP and low in the
day's range, on above-usual volume, is far more likely to be stopped later (|t| 4 to 10,
in both periods):
- **H3, confirmed:** a trade 0.25 ATR or more against it on above-usual volume had a later
  stop rate 49 to 63 points higher than the other open trades'.
- **Partly mechanical:** such a trade is already halfway to its stop.

The Nifty's own move after the entry matters less (t −3.0).

**When the stops come:** 8% (09:45 calls) and 14% (evening picks) in the first 15 minutes;
a quarter to a third more than 2¼ hours after the entry.

### The models

Each model is LightGBM, refitted every quarter on the trades before it. The rows are each
day's 10 strongest predictions a side, each with its path.

| Family | Candidates | Development | Holdout |
|---|---|---|---|
| **A1, veto before the entry** | the stop's probability above its 80th or 67th percentile (V20, V33); a regression of the net return below 0 (VR) | VR chosen for both | 09:45: **t −0.15**; evening: **t 1.47** |
| **A2, early exit** | the rest of the trade predicted below 0 or −0.1% (E0, E10); a later stop 50% or 60% likely (C50, C60) | **every one cut the net return a trade, for both sets: failed** | not run |

What VR did in the holdout:
- **09:45 calls:** it took 758 of 1,632 trades and doubled the return a trade (+0.156%
  against +0.079%). The rupees a day fell slightly (₹264 against ₹281): it skipped as much
  profit as loss.
- **Evening picks:** it took 561 of 1,576 trades and turned −₹205 a day into ₹7 a day,
  better in both 2025 and 2026. That is not significant (t 1.47, under the bar of 2.0),
  and the period had been used before.

A2 sold losers early, but also the many that recovered. Stopped trades fell from about 25%
to 15–17%, and the return a trade fell every time.

## Study B: investing, the momentum Rankings order

**The baseline, M:** each month, the 20 highest by momentum among the 500 most traded
stocks, equal weights, 0.195% costs each way. It reproduces research/momentum_upgrades
exactly: 18.4% a year in 2016–2020 and 38.0% in 2021 – September 2026, before tax.

**What counts as adverse:** a pick-month 10 points or more behind the 500. That was 15% of
pick-months in development and 18% in confirmation.

**What goes with it at the month end** (confirmed, holdout t):
- volatility over the year (t 5.9) and idiosyncratic volatility (t 6.0);
- the 12- and 6-month returns (t 4.4, 4.1), so the most extended picks;
- the largest daily return of the last month, MAX (t 3.6);
- distance above the 50-day average (t 3.1);
- beta (t 2.7) and the market's own volatility (t 2.5).

**Weaker than published:** nearness to the 52-week high (Byun & Jeon) went the right way in
development (t −2.1), but wasn't confirmed. Volume surges and delivery % showed nothing.

**After the entry:** a pick behind the 500 after 5 or 10 sessions, and trading on less
volume than usual, is much more likely to end the month adverse (|t| 3 to 15).

| | 2016–2020 | 2021 – Sep 2026 |
|---|---|---|
| M | 18.4% a year, max drawdown −38.8% | 38.0%, −25.5% |
| B1, 20 of the top 30: by the model (BM), nearest the 52-week high (BH), least volume surge (BV), least MAX (BX) | 11.0% to 15.5%, **all below M: failed** | not run |
| B2, sell when the rest of the month is predicted 3 points or more behind (XM3) | **23.1%**, −34.7% (t 2.09) | **37.4%**, −25.5% (t −1.07): **failed** |

XM3 sold about 74 holdings in development's 1,200 pick-months, and only 22 in confirmation's
1,380. Dropping the most volatile or extended picks (B1) cost 3 to 7 points a year: those
are the stocks momentum earns its return from.

## Caveats

- **Old periods:** both holdouts had been used by earlier studies.
- **Several tests:** three went to the holdout against the bar (VR twice, XM3). One pass
  would have been weak evidence; there was none.
- **Intraday data:** 197 Nifty 200 stocks since 2022.
- **Simulated fills:** the fills are the candle opens, with slippage.

## What follows

- **The tracker:** the app now records every recommendation's price, volume, VWAP and the
  Nifty at the moment it is made, and its path every 5 minutes (30 for longer ones),
  in docs/TRACKING.md.
- **Shadow models:** the A1 and A2 models, refitted on every trade to October 2026
  (`export_models.py`), score each 09:45 call and evening pick in shadow. The veto is
  scored at the entry, and the early exit at each checkpoint. No decision changes.
- **Not tested, a candidate for a new protocol:** a stop scaled to the day's own range (the
  first 30 minutes, or the opening gap), not the past 14 days' ATR. The drivers say that
  is where the stop-outs come from.
- **The evening order rule:** the evening picks' real order rule should be tested again
  before real orders run, against the paper rule.

## Files

| File | What |
|---|---|
| `PROTOCOL.md`, `PROTOCOL.sha256` | the pre-registration |
| `paths.py` | Study A's trades, labels, parameters and checkpoints from the 5-minute candles |
| `drivers.py` | Part 1: the parameters behind adverse moves, both studies |
| `models_a.py` | Study A's models |
| `momentum.py` | Study B |
| `export_models.py` | the shadow models for the app (`config/models/adverse_*_v1.json`) |
| `CHOICE.md` | the development choices |
| `dev.txt`, `holdout.txt` | the outputs |

Commands, from the repository, with DATA the scratch directory of research/ml,
research/evening and research/momentum_upgrades:

```bash
uv run python -I research/adverse/paths.py DATA OUT
L="uv run --no-project --with polars --with numpy --with lightgbm --with scikit-learn python -I"
$L research/adverse/models_a.py OUT dev
$L research/adverse/momentum.py DATA/inputs DATA/promoter/all.parquet DATA/parsed/deliv.parquet OUT dev
uv run python -I research/adverse/drivers.py OUT OUT/b_rows.parquet dev
$L research/adverse/models_a.py OUT holdout m0945=VR evening=VR
$L research/adverse/momentum.py DATA/inputs DATA/promoter/all.parquet DATA/parsed/deliv.parquet OUT confirm - XM3
uv run python -I research/adverse/drivers.py OUT OUT/b_rows_all.parquet holdout
$L research/adverse/export_models.py OUT config/models
```
