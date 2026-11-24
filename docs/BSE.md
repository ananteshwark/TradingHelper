# Stocks listed only on BSE

Until 24 November 2026 the app knew only the stocks on NSE. Its companies were built from
NSE's daily price files, so a stock listed only on BSE was in no list, on no page and in no
ranking.

How many there are: BSE's file for 20 November 2026 against NSE's for 18 September 2026.
- **1,868 equity lines trade only on BSE.** 1,471 are on the main board (groups A, B, T, X
  and XT) and 397 are SME stocks.
- **105 of them traded over ₹1 crore that day.**
- **The largest:** the National Stock Exchange itself, which can't list on its own exchange
  (₹1,729 crore that day), and NSDL.

## What is loaded

**BSE's daily price file** (`bse_cm_bhavcopy_udiff` in `config/sources.yaml`), in UDiFF format
from 1 January 2024:
- every line goes into `price_eod` as exchange BSE;
- the symbol is BSE's scrip code (`FinInstrmId`), which stays with a stock when its ISIN
  changes;
- the series is BSE's group.

**The data check** (every 30 minutes) fetches the files after the latest one loaded:
- it takes at most 25 a check, oldest first, so the history since January 2024 fills in over
  the first day or so;
- a day BSE has no file for (a holiday, or one not published yet) is skipped: BSE answers it
  with its home page, not an error.

**Storage:** each file is about 0.87 MB, so the history is about 630 MB, plus about 320 MB a
year. The cleanup switch that deletes NSE's loaded files (Settings > Maintenance,
`delete_loaded_nse_files`) deletes BSE's too. `igs rebuild` downloads them again.

## Which stocks count as listed only on BSE

The instrument master (`igs.normalize.master_db`) takes every company ISIN (INE…) in BSE's
files that never appears in NSE's.

**Groups admitted:**
- **A and B:** rolling settlement;
- **T:** trade-for-trade;
- **X and XT:** BSE's own smaller listings, rolling and trade-for-trade;
- **M and MT:** SME stocks, admitted with `include_sme`.

**Left out:**
- Z: companies not complying with the listing rules;
- P: permitted to trade, listed elsewhere;
- F and G: debt;
- mutual fund units and ETFs (INF ISINs).

Each such stock gets a company, its ISIN and BSE code over time, and a BSE listing. A stock
on both exchanges keeps NSE's prices, names and data, so no day is counted twice. When a
BSE-only stock later lists on NSE, it moves to NSE's prices.

Settings > Universe: `include_bse_only`, `bse_groups`, `bse_sme_groups`.

## Splits and bonuses

**The problem:** NSE's corporate actions don't cover these stocks, and BSE's file does not
adjust its previous close on an ex-date. In BSE's November 2026 files, CONCORDBIO's 1:1 bonus
shows as ₹1,229.85 to ₹655.20, with the previous close still ₹1,229.85.

**How they are read instead** (`igs.normalize.adjust.implied_actions`):
- These stocks have price bands of at most 20% a day.
- So a close more than 22% below or 28% above the previous session's can only be a corporate
  action.
- That day's move is taken as a split or bonus, snapped to the nearest common ratio (1:2,
  1:3, 1:4, 1:5, 1:10, 2:3, 2:5, 3:5, 3:4, or a consolidation) when the rest of the move fits
  in the band. Otherwise the move itself is used.

**From BSE's November files:**

| Stock | Close to close | Read as | Its ex-date move |
|---|---|---|---|
| CONCORDBIO | ₹1,229.85 to ₹655.20 | 1:1 bonus (½) | +6.5% |
| GNRL | ₹106.23 to ₹12.23 | 1:10 split | +15% |
| KMEW | ₹3,149.70 to ₹655.45 | 1:5 split | +4% |
| PASHUPATI | ₹83.80 to ₹35.05 | 3:2 bonus (⅖) | +4.6% |

**What it misses:**
- A ratio inside the band, such as a 1:4 bonus (⅘), isn't seen.
- A rights issue is read as the nearest ratio.
- In both cases the error is at most that day's move.

The same correction applies where the app measures a stock's move from its previous close:
the AI calls' record, results reactions and similar (`igs.assistant.calls._price_rows`).

## In the app

- **Stock page:** a BSE-only stock is found by its name or its BSE code (e.g. 544937 for the
  National Stock Exchange). Its price chart uses BSE's closes.
- **Rankings:** scored with the same rules as any stock, on BSE's prices. They need:
  - **a market cap** of ₹300 crore or more;
  - **8 quarters of results.**

  NSE's filings don't cover these stocks, so both come from a Screener.in export (by BSE
  code). The Screener download (`igs screener backfill`) takes them first, since they have
  none: their figures are used as the company's own, with nothing to check them against.
  Until then a stock shows as left out of the universe, with the reason ("market cap
  unavailable" or "only 0 quarters filed").

  The test: the synthetic market's CYCL, copied as a BSE-only company with the same prices
  and filings, gets CYCL's score and tier, its 1:1 bonus read from its prices
  (`tests/test_bse.py`).
- **Not affected:**
  - the momentum paper tracks and the Plan, which pick from Nifty 200 members;
  - the backtests, which stay on NSE, where the history is;
  - intraday trading, F&O, and bulk and block deals (NSE's).
- **Not loaded for these stocks yet:** BSE's own announcements, results filings,
  shareholding and corporate actions.

## Deploying

1. Run `igs-update`. It runs the look-ahead gate: `igs.normalize.adjust`, `igs.pit.loader`
   and `igs.universe` changed.
2. The next data check verifies the source and starts fetching. To fill the history at once:

   ```bash
   uv run igs sources verify bse_cm_bhavcopy_udiff
   uv run igs sync             # each run fetches 25 more days
   ```
