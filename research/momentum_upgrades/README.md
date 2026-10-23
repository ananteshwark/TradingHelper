# Momentum upgrades from the published evidence, tested after tax

The owner asked for a review of everything published on investing and trading, and
whether the app's results could be improved. The review ("Published evidence to improve
returns", kept outside the repository) found the best-supported untested ideas were fixes
to the one signal that works here, momentum, rather than new signals. Five were tested
against the Rankings order (momentum, top 20), after costs **and after Indian tax**.

**The rules:**
- The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was written before anything
  was run. Its first commit was lost unpushed with a cloud container; it was recommitted
  unchanged, with the same SHA-256 (dc6d90e).
- The choice (`CHOICE.md`, 76dc942) was pushed before the confirmation period was run,
  once.

**Verdict:** nothing passed, so nothing in the app changes.
- **T, managing the top 20 for tax**, came closest: 2.2 points a year better than the
  Rankings order after tax in 2021–2026. Its t was 2.28, against the 2.5 required.
- **Its gain came from one rule:** not trimming winners back to equal weight each month.
  That raised the return before tax too, so it is mostly not a tax effect.

## What was tested

Each month end, in the 500 most-traded stocks (delisted ones included). All the
strategies hold 20 stocks.

| | What it holds | Published evidence |
|---|---|---|
| **M**, the Rankings order | The top 20 by momentum (6- and 12-month return over volatility), equal weights | |
| **R**, residual momentum | The top 20 by the past year's returns the market doesn't explain, over their volatility | Sharpe 1.65 against 0.57 across 48 countries (Hanauer & Windmüller 2023) |
| **R0** | As R, without dividing by volatility | Indian momentum pays most in volatile stocks (Ansari & Khan 2012) |
| **F**, frog-in-the-pan | Of the top 40, the 20 that rose on the most days | Held out of sample in emerging markets (Goyal, Jegadeesh & Subrahmanyam 2025) |
| **E**, results reaction | Of the top 40, the 20 the market rewarded most around their latest results | Brandt et al. 2008 |
| **T**, managed for tax | M's top 20: no monthly trims (none above 10%); keep a slipped winner about to turn long-term; harvest losses and the yearly ₹1.25 lakh exemption | Israel & Moskowitz 2011 |

**How tax is counted:**
- **Lots:** every strategy runs as tax lots, starting from ₹10 lakh, and sales take the
  oldest lots first.
- **Rates:** today's rules in every year: 20% on short-term gains, 12.5% on long-term gains
  above ₹1.25 lakh, set-offs, and losses carried forward 8 years.
- **"After tax":** what the portfolio would be worth if sold at the period's end, after
  that tax.
- **Costs:** every rupee traded pays 0.195% each way.

## Results

Each cell gives the return a year after costs, before tax, then after tax.

| | Development 2016–2020 | Confirmation 2021 – Sep 2026 | After tax against M a year: development, confirmation (its t) |
|---|---|---|---|
| **M, the Rankings order** | 18.4%, **15.0%** | 38.0%, **30.9%** | |
| R, residual momentum | 15.3%, 12.1% | 31.0%, 25.2% | −3.1, −5.2 points (−1.06) |
| R0, unscaled | 5.6%, 4.3% | 22.8%, 18.6% | −8.4, −10.0 points (−1.80) |
| F, frog-in-the-pan | 25.1%, 20.7% | 29.9%, 24.2% | +3.9, **−5.7** points (−1.49) |
| E, results reaction | 17.8%, 14.3% | 36.4%, 29.4% | −0.8, −1.4 points (−0.34) |
| **T, managed for tax** | 19.9%, **17.1%** | 40.6%, **33.6%** | +2.0, **+2.2** points (**2.28**) |
| M in a fund wrapper (illustration) | 18.0%, 16.5% | 37.6%, 35.0% | |

Only F and T went to confirmation; R, R0 and E were behind M in development
(`CHOICE.md`). The confirmation rows for R, R0 and E are shown for completeness.

**Residual momentum (R, R0)** lost to plain momentum in both periods.
- It did soften the bad years, as the papers say: 2018 +4% against −14%, and a worst fall
  of −23% against −25% in 2021–2026.
- It gave up far more in the strong years.
- Without the volatility scaling (R0) it was worse still.

**Frog-in-the-pan (F)** reversed.
- It was the best variant in 2016–2020 (+3.9 points a year after tax).
- In 2021–2026 it was 5.7 points a year behind, losing most in 2022 (−7% against +7%),
  2024 and 2026.
- Both periods together: −1.3 points a year (t −0.43).

**The results reaction (E)** changed little in either period.

**Managing for tax (T)** was ahead in both periods, though not reliably enough to pass.
- **Confirmation:** 33.6% a year after tax against 30.9%, with the best Sharpe ratio (1.25
  against 1.19). Its deflated Sharpe ratio, counting the six strategies, was 0.98.
- **Both periods together** (descriptive, not part of the test): +2.1 points a year
  after tax, t 2.02.
- **The gain is the first rule alone,** letting winners run instead of trimming them back
  to 5% every month:
  - not trimming realises fewer short-term gains;
  - it also leaves more money in the strongest stocks. The return before tax rose too
    (40.7% against 38.0%), so most of the gain is not a tax effect.
- **The other three rules barely acted:**

| Rule | 2016–2020 | 2021–2026 |
|---|---|---|
| Holding a slipped winner to the 12-month line | 5 times | never |
| Harvesting a loss before it turned long-term | never | never |
| Using the yearly exemption | never | 3 times |

Top-20 momentum holdings rarely last a year; a sold rupee had been held 2.3 to 2.7
months on average.

## What tax costs the Rankings order

- **The cost:** tax took 3.4 points a year in 2016–2020 and 7.1 points in 2021–2026
  (38.0% to 30.9%).
- **Why:** almost every gain was short-term (1% long-term in 2021–2026).
- **The fund wrapper:** the same strategy inside a fund (no tax inside the fund under
  s.10(23D); 0.3% a year of expenses; 12.5% once on redemption) would have kept 35.0%, 4
  points a year more than holding it directly.
- **That is an illustration, not an option on offer:**
  - no fund runs this strategy;
  - the momentum index funds that exist follow different rules and made much less over
    the same years (the review found the Nifty200 Momentum 30 at 9.3% a year over five
    years to September 2026).

## Against the app's goals

- **Investing:** the Rankings order stays as it is.
  - None of the published refinements beat it here after tax.
  - Not trimming winners is the one lead. It was ahead in both periods but short of the
    bar, so it is not adopted.
- **Booking profit near the top:**
  - the tests found no exit rule to replace selling when a stock leaves the top 20;
  - holding to the 12-month line rarely had anything to act on.
- **Trading:** nothing here; these are monthly portfolios.

## Limits

- **The data was rebuilt** from NSE's archives after the original scratch files were lost
  (`CHOICE.md`):
  - it reproduces the originals: the same 6,507,889 price rows, and M's 2016–2020 figures
    within 0.2 points;
  - ml_rating's universe had not applied the "200 of the past 252 sessions" rule its
    protocol states, and the rebuild follows the data (`inputs.py`).
- **Results broadcasts** come from NSE's listings, by fetch_files.py's choice, without
  downloading the filings. They cover 59,014 quarters, against 57,436 parsed before.
- **Tax:** only the rates and set-offs.
  - No cess or surcharge.
  - Gains are on prices; the 0.39% costs are not deducted from them.
  - Dividends are left out.
  - Today's rates are applied to every year.
- **Momentum's own history:** momentum was chosen as the Rankings order on these years,
  so M is favoured in both periods.

## Files

- `upgrades.py`: the strategies, the tax ledger and the summary. Output in `dev.txt` and
  `holdout.txt`.
- `inputs.py`: the universe, momentum percentile and results broadcasts, rebuilt.

They ran with `uv run --no-project --with polars --with numpy python -I` on the scratch
data, which is not kept in the repository.
