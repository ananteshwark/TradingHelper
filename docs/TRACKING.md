# The recommendation tracker

Every recommendation the app makes is recorded with the market at the moment it was made,
and its price and volume are followed after it. The record shows how far each one went
against its side before it worked, or didn't, and what the volume was doing then. It is
what research/adverse measured adverse moves on in the past, from candles. From here it is
the forward record to retrain and check its models on.

Nothing here places an order or changes a recommendation.

## What is tracked

| Source | What | Followed |
|---|---|---|
| 09:45 model | its buys and short sells (docs/INTRADAY_ML.md) | from the decision to 15:15 |
| Evening picks | the next session's buys and short sells (docs/INTRADAY_EVENING.md) | that session, from 09:20 (when real orders enter) to 15:15 |
| Spike picks | the spike model's trades (docs/SPIKES.md) | from the decision to 15:15 |
| Scanner calls | the five-minute scanner's buy and sell calls (docs/INTRADAY.md) | from the call to 15:15 |
| AI calls | buy and sell calls, not holds | to the end of their horizon, at most a year |
| Momentum paper | each track's holdings at each rebalance | from the entry for 35 days |

## At the recommendation

`igs track` (`igs-track.timer`) runs every minute from 09:00 to 15:59 on weekdays, and
every 15 minutes from 16:00 to 21:45 for the evening's recommendations. It finds those
made since its last run and records, at once, Upstox's quote of each stock and of the
Nifty 50:

- the last traded price;
- the day's volume so far, and against its usual by that time of day (below);
- the day's average traded price (VWAP), high and low, and the previous close;
- the Nifty 50's level.

So a recommendation made during market hours is priced within a minute; the price the
source itself recorded (the 09:45 model's decision price, an AI call's entry) is kept
beside it. One made after the close (the evening picks, most AI calls) is priced at the
last close, marked `close`, and its path starts in the next session. A capture more than two
minutes late says so, and one whose quote failed is marked `failed`: its path then starts
from the first tick that gets a quote.

**Relative volume:** for an intraday recommendation, the stock's last 20 complete
sessions of five-minute candles are read once, and the day's volume so far is compared
with their mean at the same time of day. 2.0 means twice the usual volume by then.

## Afterwards

- **Intraday recommendations:** a tick every 5 minutes from the entry to 15:15.
- **Longer ones:** every 30 minutes during market hours for their first 30 days, then one
  a day near the close, to the end of their horizon.

Each tick records the price, the day's volume, VWAP, high and low, the relative volume
and the Nifty 50. The recommendation keeps a summary of its path, in % from the price
captured and signed for its side (+ is in its favour):

- **MAE**, the worst move against it so far, when it came, and the relative volume then;
- **MFE**, the best move in its favour, and when;
- the last price.

These are measured at the ticks, so a move between two ticks that reverses before the
next isn't seen. research/adverse measured the same thing on every five-minute candle.

The records are kept: the cleanup (docs/MAINTENANCE.md) never removes them. About 75 ticks
an intraday recommendation and 300 a month a longer one, a few megabytes a year.

## Where to see it

The **Tracker** page lists the recommendations, newest first, with the price at the
recommendation, the latest, and the worst and best moves so far, and plots one's path
with its volume.

## Shadow predictions

The 09:45 calls and the evening picks are also scored by research/adverse's models, and
the scores are kept beside each call (`shadow`). **They change nothing:** none of the
models passed its holdout.

- **At the entry** (the call's first tick), A1:
  - the predicted net return of the trade after costs; the study's veto (VR) would skip a
    call when it is below 0;
  - the chance its stop is hit.

  Its inputs are the call's own model inputs, its prediction and rank, the day, the Nifty
  50's gap, move and last days, and breadth: the share of the Nifty 200 above their
  previous close.
- **At 15, 30, 45, 75 and 135 minutes after the entry,** while the stop hasn't been hit,
  A2:
  - the predicted rest of the trade before costs;
  - the chance the stop is hit later.

  Its inputs add the path so far: the move, the best and worst moves, the last 15 minutes,
  the volume since the entry against its usual, the distance from VWAP, the place in the
  day's range, and the Nifty's move.

**Where the models come from:** `config/models/adverse_m0945_v1.json` and
`adverse_evening_v1.json`, trained by `research/adverse/export_models.py` on every trade
from January 2023 to October 2026. The app evaluates the trees itself; the export checks
that it agrees with LightGBM, missing inputs included.

**How the path differs from the research's:** the research measured it on every 5-minute
candle, the tracker on its ticks, so its best and worst moves can be less extreme.

**What happens next:** once the tracker's record covers a few months, the models can be
refitted on it and compared, forward, with the research's result. A model is offered as a
switch only if its forward record agrees with a pass.

## What research/adverse found

**What drives adverse moves held out of sample** (2025 – October 2026):
- **Intraday stop-outs** come with the day's own volatility, against a stop set by the
  past 14 days' ATR. The share stopped rises from about 13% to 43% across the fifths of
  the first half hour's range over the ATR, and from 12% to 40% across those of its
  volume against usual. It falls from 36% to 17% across those of the ATR itself.
- **Not the direction:** trading against the Nifty, a weak prediction, or a stretch from
  VWAP didn't matter.
- **Momentum's worst months** come with the most volatile, beta-heavy and extended picks.

**Why the models didn't pay:**
- The same volatility makes the big winners, so the vetoes skipped as much profit as loss.
- The early exits cut the trades that would have recovered.
- No model earned more after costs than the calls as they are.

**The evening picks lose with the real order rule:** in at 09:20, a stop at half the ATR,
out at 15:00. That lost 0.06% a trade in 2025–2026; see research/adverse/README.md.

## Commands

```bash
uv run igs track          # one step: register new recommendations, take due ticks
```

`scripts/install-schedules.sh` installs `igs-track.timer` with the other schedules;
Settings > Schedules runs it now.
