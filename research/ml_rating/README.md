# A learned stock rating against the app's rule score and momentum

The owner holds that fixed rules don't work in the stock market and that the app should
rate stocks with intelligence. This study tested that on 2012–2026 point-in-time data. The
protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed before any feature or
model existed; `CHOICE.md` before the holdout was run.

## Data

**Results.** NSE's quarterly results archive gave 57,436 standalone filings for 1,322
companies, 2011 to September 2026, delisted and merged companies included.
- **Formats:** HTML pages to late 2017, XBRL after, and Integrated Filing XBRL from
  2025. `parse.py` reads all three into rupees.
- **Timing:** each quarter is known from its first broadcast time.
- **Two gaps at NSE itself:** a sixth of companies have no machine-readable March 2018
  quarter and nearly a third no June 2018 quarter (TCS and Infosys among them). About a
  fifth have no June 2022 quarter (Ambuja Cements among them). Their growth inputs are missing for
  a year after each gap. Nothing is imputed.
- **Unit errors:** 214 quarters whose revenue was over 20 times off their neighbours
  lost their amounts.

**Prices.** Every EQ stock's adjusted daily prices from the bhavcopies (research/ml).

**Universe.** Each month, the 500 most-traded stocks.

**Inputs.** The app's growth, quality (margin, its trend, interest cover), valuation and
momentum factors as `features.py` rebuilds them, plus profit surprise, 1-month return,
distance from the 52-week high, size, liquidity and delivery %. Balance sheets and
ownership have no history here.

## Results

Monthly return over the equal-weighted universe after 0.39% round-trip costs. The
t-statistic is on the monthly series; 2 or more is the bar.

**Development (2016–2020)**, where the models were chosen (`dev.txt`):

| | Learned (LightGBM) | Learned (ridge) | Rule score | Momentum |
|---|---|---|---|---|
| Trading: top 20, monthly | +0.30% (t 0.7) | +0.23% (t 0.5) | +0.35% (t 0.6) | +0.75% (t 1.2) |
| Investing: 12 cohorts of the top 50 | +0.01% (t 0.0) | +0.15% (t 0.6) | +0.16% (t 0.4) | +0.21% (t 0.7) |

**Holdout (January 2021 to September 2026), run once** (`holdout.txt`):

| | Learned (chosen) | Rule score | Momentum |
|---|---|---|---|
| Trading | +0.34% (t 1.1), LightGBM | +0.04% (t 0.1) | +1.10% (t 1.9) |
| Investing | −0.26% (t −0.9), ridge | −0.19% (t −0.6) | +0.52% (t 2.0) |

**Neither learned rating passed.** The trading one beat the rule score but not momentum,
and not reliably. The investing one lost to the universe.

**The rule score had no edge either.** With the app's pillar weights, its top stocks did
about as well as the universe in both periods. This is the first test of those weights
on real data. It is approximate: the app also ranks within sector peers and uses inputs
missing here.

## Which pillars carry it

Descriptive, not part of the protocol (`pillars.txt`). Each pillar's own top 20 (trading)
and top-50 cohorts (investing):

| Pillar | Trading 2016–2020 | Trading 2021–2026 | Investing 2016–2020 | Investing 2021–2026 |
|---|---|---|---|---|
| Momentum (6- and 12-month return over volatility) | +1.05% (t 1.8) | +1.21% (t 2.4) | +0.45% (t 1.6) | +0.46% (t 2.0) |
| Growth | −0.74% (t −1.9) | +0.27% (t 0.6) | −0.28% | +0.03% |
| Quality | −0.49% | −0.20% | −0.09% | −0.43% (t −1.9) |
| Valuation | −0.53% | +0.31% | −0.21% | +0.15% |
| Low volatility | +0.04% | −0.92% (t −1.8) | +0.17% | −0.74% (t −1.8) |

Only momentum was positive in both periods, at both horizons. The other pillars diluted
it, so the composite ended up near zero.

Reported growth did not predict returns. By the time results are published, the
improvement is, on average, already in the price.

## What it means

- **Rules vs intelligence is the wrong line.** The hand-weighted score failed, and a
  model learning from the same 14 years did not do better than plain momentum. Momentum
  is itself a simple rule.
- **What held up is the stock's own price trend** over 6–12 months. The app already
  tracks it forward on the Momentum page.
- **By the protocol, nothing in the app changes.** Re-weighting the ranking towards
  momentum would be a choice made after seeing these results. It needs the owner's
  decision and its own forward record.

## Files

| Step | File |
|---|---|
| Listings | `fetch_ranges.py`, `fetch_if.py` |
| Filings | `fetch_files.py` |
| P&L lines | `parse.py` |
| Month-end inputs and forward returns | `features.py` |
| Walk-forward models, rule score, portfolios | `model.py` |

The scripts ran in a scratch environment with
`uv run --with lightgbm --with scikit-learn python -I`. The downloaded data is not kept in
the repository.
