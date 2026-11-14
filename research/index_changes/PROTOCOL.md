# Joining the Nifty 50 or Nifty Next 50: protocol

Written and fingerprinted before any return around an index change was computed. Only the
press releases' format had been looked at.

## Question

Index funds and ETFs must buy a stock joining the Nifty 50 or the Nifty Next 50 at the close
before the change takes effect, on a date announced weeks earlier. Does buying at the open
after the announcement and selling at that close beat the market after costs?

## Data

- **Index changes:** NSE Indices' press releases "Change in indices", "Replacement(s) in
  indices" and similar, listed on niftyindices.com/press-release with their dates, from
  2011 to 2026. From each, the stocks included in and excluded from the Nifty 50 (also
  named CNX Nifty and S&P CNX Nifty) and the Nifty Next 50 (CNX Nifty Junior), and the date
  the change takes effect ("with effect from"). Other indices are ignored. A change in
  more than one release (a correction, a repeat) counts once, from its first release.
- **Prices:** NSE's daily bhavcopies, linked and adjusted for bonuses and splits as in the
  promoter study (`research/promoter/build_all.py`). The benchmark is NIFTYBEES, the
  largest Nifty 50 ETF, from the same files.

## Events

- **N50:** a stock included in the Nifty 50.
- **NN50:** a stock included in the Nifty Next 50 that the same release does not move out of
  the Nifty 50 (a stock joining from outside the two indices).

## Trade

- **Entry:** the open of the first session after the release's date. Releases come out at
  any time of day, often after the close, so the same day's open can't be assumed.
- **Exit:** the close of the last session before the effective date, when index funds buy.
- An event whose entry would not come before its exit is dropped.
- **Abnormal return:** the stock's return less NIFTYBEES's over the same open-to-close
  window.
- **Costs:** `igs.momentum.cost_pct` at the entry's raw price (about 0.4% a round trip).
- **Also reported, not tested:** the abnormal return from that close to the close 20
  sessions later, which shows whether the move reverses.

## Statistic and periods

Events in the same release are averaged; each release counts once; t = mean / standard
deviation x sqrt(releases).

- **Development:** releases from 1 January 2011 to 31 December 2019.
- **Confirmation:** releases from 1 January 2020 to 30 September 2026, run once for each
  event set whose development mean net abnormal return is positive.

## Passing

An event set passes if its confirmation mean net abnormal return is positive with t at least
2. Two sets are tested; neither threshold is relaxed for that.

## Checks before results

1. Events found per year and per index, and the releases with none.
2. The share of included stocks matched to a price on the entry day.
3. Releases whose effective date could not be read.
