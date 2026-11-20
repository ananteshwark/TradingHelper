# Choices, made on the development periods only

Written before either confirmation period was run (`intraday_dev.txt`, `daily_dev.txt`).

## Intraday (tests A and B): failed on development

Every variant lost money after charges, March 2022 – December 2024:

| Variant | Trades | Net a trade | Gross a trade | Daily t |
|---|---|---|---|---|
| A1 board, both sides, at 09:50 | 4,145 | −0.140% | −0.019% | −5.67 |
| A2 board, dominant side only | 1,216 | −0.099% | +0.021% | −1.84 |
| B1 first return to the block | 1,157 | −0.101% | −0.006% | −6.47 |
| B2 the same, dominant side only | 331 | −0.069% | +0.027% | −2.43 |

- **Candidate:** A2, with the highest t (−1.84). It fails the bar of t ≥ 2.5.
- **The board:** its picks were no better than the other F&O stocks after 09:50. 23% ended
  the day in their side's top fifth, against 20% by chance; their gross excess over the
  F&O average was −0.02% (t −0.90).
- **The zone:**
  - Most of the time the stop was hit: 705 of 1,157 trades stopped, 322 reached the target.
  - Entering at the zone instead of at 09:50 changed a gross −0.019% a trade into −0.006%.
    Both are about zero before charges, and charges of about 0.1% a trade make both
    losses.

The confirmation period is still run once, for the record. It can't change this verdict.

## Daily (test C): C1 passes development

| Variant | Trades | Net excess a trade | Beat the universe | Monthly t |
|---|---|---|---|---|
| C0 buy the next open after the break | 5,581 | −0.28% | 34% | −0.78 |
| C1 buy the first return to the block | 4,165 | +0.58% | 35% | 2.37 |

- **Candidate:** C1, with t 2.37, at or above 2.2.
- **Where its average comes from:** a few large winners. The median trade lost 1.63% to the
  universe, and 78% of trades were stopped by a close below the block. Without the best
  50 trades (1.2%) the mean turns slightly negative. With the top and bottom 1% trimmed
  it is +0.45%.
- **Data checks:** the daily highs and lows agreed with every open and close (2.1 million
  rows).
- **Periods:** they go by entry day, as the protocol says. The first run used the break
  day, which moved 24 trades; the numbers above are by entry day.

Confirmation to run once:

```
daily.py ... holdout       (C1, with C0 reported alongside)
intraday.py ... holdout    (for the record; the intraday tests have already failed)
```
