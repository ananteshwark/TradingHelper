# Machine-learning intraday calls

The page **ML intraday** and Telegram track, forward, the one intraday model that passed
a test fixed in advance. It is separate from the rule-based intraday calls, which are
switched off. The record is kept on paper. When the owner turns on real orders, each call
can also be approved for a real Upstox order (see [Real orders](#real-orders)).

## Why this model

The owner asked for a predictive model after rule-based intraday calls kept hitting their
stops. Twelve rule-based strategies had failed. Every rule-based intraday strategy lost
money after costs in every year from 2022 to 2026, because their moves were a third of the
0.12% round-trip cost ([MOMENTUM.md](MOMENTUM.md)).

The study (protocol, choice and results in `research/ml/`) was written down before any
model was fitted:

- **Data:** Upstox 5-minute candles for 197 Nifty 200 stocks and the Nifty 50, February
  2022 to 8 October 2026. These are today's index members, a survivorship bias that
  favours buying, not the short sells this model profits from.
- **Decision:** at 09:45, after the first six candles have closed. The target is the
  return from the 09:45 candle's open to the 15:15 candle's open.
- **Inputs:**
  - the first 30 minutes: gap, return, range against the 14-day ATR, volume against the
    same 30 minutes' 20-day average, distance from the VWAP, where it closed in its range;
  - the stock's last day (return, close location, last hour, open to close) and its 5- and
    21-day returns, ATR, distance from its 20-day high and low, traded value, and 5-day
    overnight and intraday returns;
  - the Nifty 50's gap, first 30 minutes, last day and 5 days, and the day of the week.
- **Models:** ridge regression and gradient-boosted trees (LightGBM) with fixed settings.
  They are retrained every quarter on all earlier days. 2023–2024 is the development period;
  January 2025 to October 2026 is a holdout, evaluated once.
- **Trades:** each day, up to k buys among the highest predictions and k short sells among
  the lowest. Four variants (k = 3 or 10, with or without a 0.15% minimum predicted move)
  times two models gave eight candidates.
- **Costs:** 0.02% slippage each side plus Upstox intraday charges on ₹1 lakh a trade.
  Stocks priced above ₹1 lakh are skipped, as are those averaging under ₹50 crore a day.

**Choice (development, 2023–2024):** the trees, three a side, only predictions of at
least 0.15%. Results:

- +0.18% a trade before costs and +0.06% after;
- profit factor 1.07; t 1.1, weak;
- 2023 negative, 2024 positive.

The predictions ranked the day's stocks with a rank correlation of 0.034 (t 6.0).

**Holdout (January 2025 to October 2026, run once):** it passed every rule set in advance.

| | |
|---|---|
| Trades | 1,598 (3.7 a day) |
| Per trade, before / after costs | +0.26% / +0.13% |
| Profit factor | 1.19 |
| Net per day (₹1 lakh a trade) | ₹479 (t 2.4) |
| 2025 / 2026 per day | ₹628 / ₹285 |
| Buys / short sells, per trade after costs | −0.02% / +0.25% |

**What it does.** Most of its signal comes from the market: the Nifty's first 30 minutes,
gap and recent days. Within a day it short-sells stocks that:

- gapped up after a 5-day rise (median +4%);
- then fell in the first 30 minutes on heavy volume (1.6× normal);
- and closed near the low of that range, below the VWAP.

It buys the mirror case. After removing the Nifty's own move that day, the holdout result
is still +0.12% a trade (t 2.4): it picks stocks, not just the market's direction.

**Its weaknesses:**

- The profit is lumpy: February 2025 and January 2026 made most of it. March to September
  2026 lost money overall, and the drawdown reached ₹55,000 on ₹1 lakh a trade.
- With no stop, one trade can lose 15% or more in a day.
- At 0.05% slippage a side it still earns +0.07% a trade; at 0.10% it loses.
- Entering one candle later (09:50) kept +0.12%.
- The 2026 part of the holdout was weaker than 2025, after removing the market as well.
- The holdout pass partly reflects a lucky random seed. Rerun with six other LightGBM
  seeds, the same rule averaged Rs 393 a day (t 1.94, range 1.06 to 2.60); three of seven
  seeds pass t >= 2, though all are profitable in both years
  ([research/seq](../research/seq/README.md)).

A forward record is the honest next test.

The same study tested a swing model on every NSE stock since 2012, delisted ones included.
It beat the 250 most-traded stocks by 0.55% a month after costs in 2021–2026, short of its
pre-set bar (t 1.6 against 2.0), and plain momentum did as well with far less trading
([research/ml](../research/ml/README.md)). It is not tracked.

## The paper calls

The every-five-minutes intraday job (`igs-intraday.timer`) runs this first, then its usual
scan:

- **From 09:00:** it stores each Nifty 200 stock's last 22 full sessions, summarised from
  Upstox 5-minute candles, in `ml_intraday_session`. One request a stock a day; about 45
  days on the first run.
- **09:45 to 09:55, once:**
  - fetches today's candles for the liquid Nifty 200 members;
  - computes the same inputs and runs the model;
  - records up to three buys above +0.15% and three short sells below −0.15%
    (`ml_intraday_pick`) and sends a Telegram note.
  - Each call enters at the open of the first 5-minute candle after the decision (09:50
    when it finishes at 09:46), not the 09:45 open the backtest used, since that is when a
    call can first be acted on.
- **From 15:20:** each call exits at the 15:15 candle's open. The result after slippage and
  charges on ₹1 lakh is recorded, with a Telegram note of the day and the total. Calls the
  job couldn't close that day are closed on a later run from that day's candles.

The model is fixed: `config/models/intraday_ml_v1.json` holds its 400 trees, trained on
every day to 8 October 2026. The app evaluates them itself, without LightGBM. Retraining
would make a new model with its own record. `research/ml/export_model.py` rebuilds it, and
`check_ml_features.py` checks that the app computes the same inputs as the research.

It needs an Upstox access token (Settings, Upstox tab) and the Nifty 200 list, which the
check loads daily. Without them the page says why there are no calls.

What profitable intraday traders do differently, and how that applies to these calls
(trade size, stops, limit entries, how many calls before judging): [WINNING_TRADERS.md](WINNING_TRADERS.md).

## Real orders

At the owner's request, each call can be traded with real money, approved one at a time.
Nothing is ordered without an approval.

**Switching it on.** In **Settings → Intraday trading**, tick both:
- **Enable live approved trading**;
- **Real orders for the ML calls**.

A standard Upstox trading OAuth token must be saved (Settings → Upstox).

**Approving.**
- **Telegram:** after the 09:45 decision, each call gets its own message. Reply APPROVED
  to the message itself.
- **Page:** on the **Trading** page's **ML intraday** tab, under Real orders, choose the call, tick the
  confirmation and press **Place the order**.

Approvals are accepted until 10:00 IST.

**The order.** One Upstox intraday GTT with two legs:
- **Entry:** a limit 0.1% past the live price at approval (above it for a buy). It fills
  at once, like the market entry tested, but never further away. Unfilled, it is
  cancelled after the minutes in the settings, by 15:00 at the latest.
- **Stop:** half the stock's 14-day ATR from the live price, in whole ticks away from the
  trade. This was the development choice in [WINNING_TRADERS.md](WINNING_TRADERS.md):
  it cut the worst trade from −16% to −5%.
- **No target.**
- **Square-off at 15:00 IST:** the app cancels the GTT, and only once its stop can no
  longer fire, closes what is still open with a market order.

**Why 15:00 and not 15:15.** Since NSE's closing auction began on 3 August 2026, Upstox
squares off intraday positions in F&O stocks at 15:10, for a fee, and refuses new intraday
orders in them after 15:09 ([Upstox](https://upstox.com/help-center/252251/)). Most Nifty
200 stocks are F&O stocks.

**Refused when:**
- the live price is further from the price the model saw (the 09:40 candle's close)
  than **Maximum price deviation** in the settings (0.5% by default);
- the stock already has an intraday order that day;
- it would pass **Maximum trades per day** or **Maximum daily gross order value**;
- Upstox does not allow the stock intraday.

The quantity is the smaller of **Amount per trade** and **Maximum loss per trade at the
stop** divided by the stop distance.

Size matters. The model was tested at ₹1 lakh a trade, and below about ₹50,000 charges
take more than its edge. The messages and the page warn when your settings make a trade
smaller than that. At the default ₹10,000 and ₹100 the calls can't pay. To trade them as
tested, raise both settings: for example ₹1 lakh a trade and ₹1,000 at the stop. Upstox
blocks only the intraday margin for it, which depends on the stock (often about a fifth
of the order value).

**What can go wrong, and what the app does:**
- **The stop fires:** the trade closes at the stop. Its net P&L, from Upstox's fills less
  estimated charges, is recorded and sent, as for any trade.
- **Upstox won't cancel the stop at 15:00:** its documentation covers cancelling a GTT only
  before it triggers. A second exit could then reverse the position, so the app sends none.
  It tells you that Upstox's own square-off will close the trade at 15:10, or that you can
  cancel the GTT in Upstox and close the trade yourself.
- **The square-off order is refused, or its answer is lost:** the app reports it and never
  resends.
- **The app is down at 15:00:** Upstox's square-off at 15:10 closes the trade, for its fee.
- **Real fills differ from the test:** the test assumed 0.02% slippage a side; at 0.10%
  the edge goes.

**Tested this way** ([research/ml_live](../research/ml_live/README.md)). These rules were
fixed before the run, on the same years as the model's own test:

| | 2023–2024 | 2025–Oct 2026 |
|---|---|---|
| Entry 09:50, exit 15:00, with the stop | +0.05% a trade after costs (t 1.0) | +0.08% (t 1.7) |
| Entry 10:00, exit 15:00, with the stop | +0.01% (t 0.3) | +0.06% (t 1.3) |
| The paper rule, for comparison: 09:50 to 15:15, no stop | +0.03% | +0.12% |

It is weaker than the paper rule, and weaker the later the approval. Short sells carry
it, and no row reaches t = 2. A few hundred real trades will say more than these tables.
The Trading page's Intraday calls tab lists every order with its net P&L.

## Input validity and forward evidence

The ML decision requires the stock's previous 22 session dates to match the
index's observed session dates. A failed history refresh cannot silently reuse
an older window. The actual scan must finish before 09:55 IST; an expired scan
records no picks. Withheld histories and expired decisions appear in the day note.

Forward evidence uses total net rupees per complete decision day, including
no-call cash days, rather than treating same-day calls as independent. A day with
open or missing-data calls is unresolved, and withholds the descriptive
t-statistic. Neither the statistic nor the approximate 250-call planning figure
certifies skill. See [the call-quality review](CALL_QUALITY_REVIEW.md) for the
repository evidence, limitations and remaining validation.
