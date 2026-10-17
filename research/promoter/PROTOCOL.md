# Promoters buying their own shares: protocol

Written and fingerprinted before any return after a promoter purchase was computed. Only the
disclosures' fields and counts had been looked at.

## Question

When a company's promoters buy its shares on the open market, does the stock beat the market
afterwards by enough to trade, after costs? Insider purchases predict returns in many
markets; Indian promoters, who usually control the company, should know most.

## Data

- **Disclosures:** NSE's insider-trading disclosures under SEBI's PIT Regulations
  (`/api/corporates-pit`, equities), downloaded month by month: November 2015 to May 2026.
  The endpoint returns nothing for June 2026 onwards; NSE moved new disclosures to another
  listing.
- **Prices:** NSE's daily bhavcopies, January 2012 to 8 October 2026, series EQ and BE,
  linked across renames and adjusted for bonuses, splits and consolidations as in
  `research/build_daily.py` (NSE's corporate-action files; unrecorded overnight moves beyond
  -40%/+60% snapped to the nearest simple ratio). Dividends are not added back.

## The event

A disclosure row with:
- person category "Promoters" or "Promoter Group";
- mode of acquisition "Market Purchase" and transaction type "Buy";
- security type "Equity Shares" and a positive value.

Rows for the same symbol broadcast before the same session's open are combined into one
event, their values summed.

**Timing:** the broadcast time (the `date` field) is when the market could know. Entry is at
the open of the first session whose 09:15 IST start is after the broadcast time.

**One at a time:** a company's event counts only if its previous counted event's holding
period has ended; promoters often buy over several weeks, and the first disclosure is the
news.

**Investable stocks only**, at the session before entry: traded on at least 200 of the last
252 sessions, close at least Rs 20, and median daily traded value over 126 sessions at least
Rs 2 crore.

## Measurement

- **Return:** adjusted open at entry to adjusted open H sessions later.
- **Benchmark:** the equal-weighted average of the same window's return across all stocks
  investable (as above) at the session before entry.
- **Abnormal return:** the stock's return less the benchmark's.
- **Costs**, a round trip: `igs.momentum.cost_pct` at the entry open's raw price (STT,
  stamp, fees, Rs 40 brokerage, DP charge, 0.1% slippage), plus more slippage for less
  liquid stocks by their median traded value: 0.3% under Rs 10 crore a day, 0.1% from
  Rs 10 to 50 crore, nothing above.
- **Net abnormal return** = abnormal return - costs.

**Statistic:** calendar time. Each event's net abnormal return goes to its entry month; each
month with events counts once, at the mean of its events; t = mean / standard deviation x
sqrt(months). Also reported: the mean per event, the share of events that beat the
benchmark, gross figures, and each year's mean.

## Variants and choice

Four variants, chosen on the development period only:
- size: every event (V-all), or events of at least Rs 1 crore (V-1cr);
- holding: H = 21 or 63 sessions.

The variant with the highest development t-statistic of net abnormal returns is the one
confirmed. If none has a positive development mean, the confirmation is not run.

## Periods

- **Development:** entries from 1 December 2015 to 31 December 2020.
- **Confirmation:** entries from 1 January 2021 to the last that completes its holding by
  8 October 2026. Run once, for the chosen variant. These years' prices were used in other
  tests here, but no insider data was.

## Passing

The chosen variant passes if its confirmation mean net abnormal return is positive with a
calendar-time t-statistic of at least 2.

## Checks before results

1. Events whose broadcast precedes the acquisition's start date: reported and dropped.
2. The median lag from the acquisition's end to the broadcast, in days.
3. The share of events matched to a price on the entry day.
