# Reversed calls: would the opposite trade do better?

The owner's view: most traders now use AI-driven algorithms, so they get the same calls the
app makes, and the volume that follows makes them fail. If so, the opposite trade should
do better: a short sell where the app says buy, a buy where it says sell.

The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed (4b05e32) before
any reversed result was computed.

**Verdict: reversing makes every call type much worse.** Nothing changes.

## Why it can't work here

A reversed trade has the original's move before costs with the sign flipped, and pays the
costs again:

- reversed net = −(the original's move before costs) − costs.

So reversal helps only if the original **loses before costs**. The app's calls that make
money do so before costs: their picks move the right way on average, and costs are what
eat into the profit. Reversed, the move turns against the trade and the costs stay.

## Results

### The intraday models whose calls can be approved for real orders

Each model's own chosen rule, picks and costs, rebuilt from the same data and code. The
rebuilt "as called" figures match each study's published development and holdout figures
exactly. Upstox intraday charges on ₹1 lakh a trade; slippage as in each study's holdout.

| | As called | Reversed |
|---|---|---|
| **09:45 ML calls**, holdout 2025 – 8 Oct 2026 | +0.13% a trade, **+₹479 a day** (t 2.37) | −0.38% a trade, **−₹1,365 a day** (t −6.74) |
| 09:45 ML calls, development 2023–2024 | +₹232 a day (t 1.06) | −₹1,249 a day (t −5.72) |
| **Evening picks**, holdout 2025 – 8 Oct 2026 | +0.14% a trade, **+₹488 a day** (t 2.02) | −0.51% a trade, **−₹1,755 a day** (t −7.21) |
| Evening picks, development 2023–2024 | +₹320 a day (t 1.13) | −₹1,857 a day (t −6.56) |

**Every year, reversal lost more:**

| ₹ a day | 2023 | 2024 | 2025 | 2026 to 8 Oct |
|---|---|---|---|---|
| 09:45 calls, as called / reversed | −66 / −945 | +529 / −1,551 | +628 / −1,530 | +285 / −1,150 |
| Evening picks, as called / reversed | +171 / −1,687 | +469 / −2,027 | +646 / −1,955 | +283 / −1,493 |

**Is crowding showing?**
- **2026 is weaker than 2025** for both models: before costs, +0.20% against +0.29% a
  trade for the 09:45 calls, and +0.27% against +0.36% for the evening picks.
- **But their picks still moved the right way** before costs, which is the opposite of
  what reversal needs.

**The latest days, 9–23 October** (ten sessions, too few to judge):
- **Evening picks:** both originals lost. The evening picks lost ₹2,160 a day, almost
  all on their buys; reversed, they made ₹863 a day (t 0.56). Ten days like this happen
  by chance often enough.
- **09:45 calls:** lost ₹494 a day as called, and ₹329 a day reversed.

### The other calls, from published results

**The six rule-based intraday strategies** (`docs/MOMENTUM.md`).
- Before costs they moved between −0.11% and +0.04% a trade, against a round trip of about
  0.12%. Reversed, each loses between 0.01% and 0.16% a trade.
- These calls barely move at all, so both directions lose to charges.

| Strategy | Before costs | Reversed, after costs |
|---|---|---|
| Opening-range breakout | +0.03% | −0.15% |
| Momentum into the close | +0.04% | −0.16% |
| Previous day's high/low breakout | −0.01% | −0.11% |
| VWAP pullback | +0.02% | −0.14% |
| Midday reversal | −0.11% | −0.01% |
| Gap fade | −0.01% | −0.11% |

**The Rankings order's top 20.**
- A month-long short isn't possible in India's cash market; it would take stock futures.
- Even with the costs given back and the futures' premium of about 7% a year credited,
  shorting the top 20 would have lost:
  - 17.5% a year in 2016–2020, winning 42% of months;
  - 27.5% a year in 2021 – September 2026, winning 33% of months.
- The top 20 itself made +18.4% and +38.0% a year after costs.

**The AI's buy/hold/sell calls** have their record only in the app's database, not here.
- The AI calls page shows it, per model. A model whose mean edge stays negative over 30 or
  more scored calls would be the one to look at reversing.

## What it means

- **Crowding doesn't show up as calls that reverse.** The app's calls lose to costs and to
  narrow edges, not to a crowd trading against them.
- **If the crowd were front-running the calls,** the moves would show up before the entry
  and the calls' own moves would shrink towards zero. Then both directions would lose to
  charges, as the rule-based strategies do, rather than the reversal winning.
- **The evening picks' losing buys in October** are worth watching on the Trading page's
  Evening picks tab. Ten days can't tell.

## Limits

- **Survivorship:** the intraday universe is today's Nifty 200. That favours buying, which
  makes the reversed buys (short sells) look a little worse than they would have been.
- **Rule-based strategies:** their trades weren't kept, so their reversal is computed from
  published averages and costs, not trade by trade.

## Files

- `reverse.py`: each model's picks as called and reversed, by period and year. Output in
  `results.txt`.

It ran on Upstox's public 5-minute candles, downloaded again with `research/seq/fetch.py`.
The features and walk-forward predictions came from `research/ml` and `research/evening`'s
own scripts. The data is not kept in the repository.
