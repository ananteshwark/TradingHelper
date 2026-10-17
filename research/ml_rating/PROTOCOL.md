# A learned stock rating against the app's rule score and momentum

Written 2026-10-16, before any model was fitted or any feature was computed. The quarterly
results it uses were still downloading.

## Why

The owner holds that fixed rules don't work in the stock market and that the app should
rate stocks and pick trades with intelligence. The app's ranking is a rule. It uses
pillar weights taken from published studies (momentum, quality, valuation and low
volatility 20% each, growth 15%, ownership 5%) and has never been tested on real data;
the app's own history starts in 2025.

This study asks two questions, on 2012–2026 point-in-time data:

1. Does a model that learns how to combine a stock's results and price history rate
   stocks better than the app's hand-set weights do?
2. Does either beat plain momentum?

An AI language model can't be tested this way: it has read what happened after any past
date. Only its forward record can judge it.

## Data

- **Prices:** NSE daily bhavcopies, 2012 to 8 October 2026, every EQ-series stock,
  delisted ones included, adjusted for corporate actions (research/ml, Amendment 1).
- **Results:** each company's standalone quarterly results from NSE's archive.
  - HTML pages before late 2017 and XBRL after; amounts are converted to rupees by the
    unit each filing states.
  - Each quarter's first filing is used, known from its broadcast time.
  - Standalone throughout, because quarterly consolidated results were not required
    before 2019. A YoY comparison is always on one basis.
- **Universe:** at each month end, the 500 stocks with the highest median traded value
  over 126 sessions, with at least 200 sessions of the past 252.

## Point in time

A month-end reading uses only results broadcast before that day, and prices to its close.

## Inputs

The same for every model and for the rule score.

- **Growth:**
  - revenue, EBITDA and profit after tax: 3- and 5-year CAGR of trailing twelve months;
  - TTM YoY of revenue and profit after tax;
  - the last quarter's YoY of revenue, profit after tax and operating margin;
  - growth acceleration over 4 quarters;
  - growth consistency over 12 quarters.
- **Quality:**
  - operating margin;
  - its 8-quarter trend;
  - interest cover.

  Return on capital, return on equity, cash conversion, debt and working capital need
  balance sheets that old results lack, so they are left out.
- **Valuation:**
  - P/E on trailing earnings against the stock's own 5-year median;
  - trailing PEG;
  - market cap to TTM EBITDA.

  Market cap is price times shares from paid-up capital over face value. Price to book
  is left out for want of book value.
- **Momentum:** 6- and 12-month return divided by volatility.
- **Low volatility:** 1-year volatility.
- **Extra inputs for the models only:**
  - the latest quarter's profit surprise (its YoY change against the mean and spread of
    the 8 before);
  - days since the last results;
  - 1-month return;
  - distance from the 52-week high;
  - log market cap and log traded value;
  - delivery %, 20 days against a year.

Ownership (promoter holding, pledges, insider buying) has no history here and is left out.

## The three ratings

- **Rule score:** each input ranked across the universe from 0 to 1, in the direction the
  app scores it. Pillars are the app's weights within them (`config/scoring.yaml`),
  renormalised over the inputs present. The composite uses the app's pillar weights
  without ownership: momentum, quality, valuation and low volatility 20/90 each, growth
  15/90. It approximates the app's score; the app also ranks within sector peers and
  uses inputs left out here.
- **Momentum:** 12-month return skipping the last month.
- **Learned:** the model's prediction. It comes in two models and two horizons:
  - **models:** LightGBM (500 trees, learning rate 0.03, 31 leaves, at least 200 per
    leaf, 80% subsampling of rows and columns, seed 7) and ridge (alpha 1) on the
    ranked inputs;
  - **horizons:** next 21 sessions (trading; the owner set one month) and next 252
    sessions (investing).

  The target is each stock's return over the horizon, ranked across the universe that
  month. It is retrained each January on every earlier month whose horizon has ended
  before that January.

## Portfolios and costs

- **Trading (21 sessions):** each month the top 20 of a rating, equal weights, held one
  month. Costs are 0.39% a round trip on the part replaced, as research/ml.
- **Investing (252 sessions):** each month the top 50 (a tenth) of a rating starts a
  cohort held 12 months. The portfolio is the last 12 cohorts, one twelfth each, so a
  twelfth is replaced each month at the same cost.

Each is measured each month against the equal-weighted universe. The t-statistic is on
the monthly excess returns after costs.

## Periods

- **Development:** January 2016 to December 2020.
- **Holdout:** January 2021 to September 2026 for trading. For investing, cohorts started
  by September 2025, whose 12 months are complete.

## Choice, on development alone

For each horizon, the model (LightGBM or ridge) with the higher t-statistic of monthly
excess return after costs, if positive. It is recorded in CHOICE.md before the holdout is
run.

## Pass, on the holdout, run once

For each horizon, the learned rating passes if all of the following hold:

- its excess return after costs is above zero with t ≥ 2;
- it is above the rule score's and momentum's, with the same portfolio and costs;
- it is positive in at least 4 of the 6 years 2021–2026 (trading), or 3 of the 5 years
  2021–2025 (investing).

## What follows

- **Pass:** the learned rating goes into the app as its own column on Rankings, with a
  forward paper track. Making it the default order is the owner's decision.
- **Fail:** it is reported and nothing changes.

The rule score's own result is reported either way. It is the first real-data test of
the app's weights, approximate as described.
