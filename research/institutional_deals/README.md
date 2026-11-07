# How prices moved after prominent institutions' block deals, 2013–2026

The institutional-deal alerts (docs/INSTITUTIONAL_DEALS.md) give a "possible impact" line.
It comes from this study. The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was
committed (010fc29) with the institution register before `impact.py` was run.

This is a description, not a strategy test.

## Data

- **Deals:** NSE's block deals, January 2013 to October 2026: 9,134 rows.
- **Institutions:** `config/institutions.yaml` matched 4,223 rows. Added up per
  institution, stock and day, they make 1,953 events; 1,501 have prices.
- **Bulk deals:** no history (NSE's listing returns at most 70 a request).
- **Domestic institutions selling in block deals:** almost never seen (12 rows in 14
  years). The sellers are mostly promoters and private equity.

## Results

Returns are against the equal-weighted 500 most-traded stocks over the same days. Each
cell gives the mean, then the t-statistic clustered by month (`results.txt` has medians,
the share up and both halves).

| | Events | On the deal day | Next session | Next 21 sessions |
|---|---|---|---|---|
| **FII buys** | 772 | +1.5% (t 4.2) | +0.2% (t 1.0) | +1.3% (t 1.2); median −0.4% |
| under 1× daily traded value | 601 | +1.2% | +0.1% | +1.0%; median −0.6% |
| 1–5× | 128 | +2.0% | +0.1% | +1.0%; median −0.6% |
| 5× and more | 37 | +4.7% (t 4.5) | **+1.3% (t 2.3)** | **+6.1% (t 2.3)**; median +0.8% |
| **FII sells** | 145 | +0.2% | +0.2% | +0.8%; median −0.6% |
| **DII buys** | 579 | +2.2% (t 4.3) | +0.3% (t 0.6) | +1.0% (t 0.3); median −0.7% |
| under 1× | 412 | +1.5% | +0.2% | +1.2%; median −0.6% |
| 1–5× | 105 | +3.3% | +0.4% | +1.2%; median −0.9% |
| 5× and more | 61 | +5.3% (t 3.8) | +0.9% (t 1.7) | −0.2%; median −1.4% |
| **DII sells** | 5 | too few | | |

## What it means

- **The move comes on the deal day.** Stocks bought by prominent institutions beat the
  market that day: 1.5% for FII buys and 2.2% for DII buys, rising to about 5% for deals
  of five or more days' trading. An alert can only arrive after the deal is published,
  when that move is already in the price.
- **After the deal day there was no reliable further move on average.** The next session
  added 0.2–0.3%. Over a month the means are about +1%, but the medians are negative:
  a few large winners carry them, and the t-statistics are near 1.
- **Exception: the biggest FII buys.** Purchases of five or more days' trading did
  better: +1.3% the next session and +6% over a month (t 2.3). But there were only 37,
  and the median month was +0.8%.
- **FII sells** were followed by nothing distinctive.

So an alert is information about who is buying, not a signal that the price will keep
rising. The app's institutional deals page keeps the record from here.

## Files

| Step | File |
|---|---|
| Protocol | `PROTOCOL.md`, `PROTOCOL.sha256` |
| Events, returns, statistics | `impact.py` (`results.txt`) |
| Figures quoted by the alerts | `config/institution_impact.json` |

It ran with `uv run --no-project --with polars --with numpy --with pyyaml python -I
impact.py` on the scratch data of `research/move_starts` and `research/ml`, which is not
kept in the repository.
