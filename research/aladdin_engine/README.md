# An Aladdin-style engine, tested against the Rankings order

The owner asked to replicate BlackRock's Aladdin, with the GitHub project REA, and use the
same kind of engine for the app's calls.

**Why not a replica:**
- REA ("Reverse Engineer Anything") is a tool for an AI agent to take apart software it
  has a copy of.
- Aladdin is BlackRock's licensed, hosted platform. There is no copy to take apart, and
  reverse-engineering it would breach its licence.

**What was tested instead:**
- How BlackRock describes using Aladdin for its Indian systematic funds, rebuilt from
  public sources and tested on 2016–2026 data.
- The protocol (`PROTOCOL.md`, sha256 in `PROTOCOL.sha256`) was committed (cc1737a)
  before anything was run.
- The choice (`CHOICE.md`, 85baac8) was committed before the confirmation period was run,
  once.

**Verdict:** it did not pass. At the owner's request it is followed anyway, on paper:
the momentum page's **Aladdin-style engine** track (`igs.risk_engine`, `docs/MOMENTUM.md`).
That track is the candidate (momentum score, λ 10). It runs over the same liquid Nifty 200
stocks as the other tracks, not the 500 most traded.

## What BlackRock says the engine does

- **Score:** JioBlackRock's funds combine a composite research score: momentum, value, low
  volatility, quality, fundamental momentum and sentiment. Their materials cite "400+
  signals across 1,000+ stocks".
- **Construction:** Aladdin turns the score into weights by weighing "alpha, risk,
  liquidity, transaction costs and constraints", with an India-specific risk model. The
  result is 50–75 stocks out of about 250.
- **Risk model:** BlackRock's risk teams describe linear factor models, covariance
  estimation, VaR and stress tests.

The exact signals, weights and limits are not public. Sentiment has no history here, so
the score uses the other five.

## The engine as built

Each month end, in the 500 most-traded stocks (delisted ones included):

1. **Score:**
   - **E1:** the mean of the five pillar percentiles (BlackRock's factors).
   - **E2:** momentum alone.
2. **Expected return:** IC 0.05 × the stock's risk × its z-score (Grinold's rule).
3. **Risk model:** about 22 industry factors and four styles (size, momentum, volatility,
   value), estimated from a year of daily returns, plus each stock's own risk.
4. **Optimizer:** maximize expected return − risk penalty − trading costs.
   - Long-only, at most 5% a stock, each industry within 10 points of the benchmark.
   - The benchmark is the universe by market cap, a Nifty 500 stand-in.
   - Three risk settings, λ 10, 25 and 60.

Everything is net of 0.39% round-trip delivery costs on what is traded.

## Results

Each cell gives the return a year, then Sharpe (over 6% cash), then the worst fall.

| | Development 2016–2020 | Confirmation 2021 – Sep 2026 |
|---|---|---|
| **E2 at λ 10, momentum score (the candidate)** | 19.5%, 0.64, −30% | 24.4%, 0.95, −22% |
| E1 at λ 10, BlackRock's composite score | 12.2%, 0.39, −30% | 16.8%, 0.69, −16% |
| **M, the Rankings order (momentum top 20)** | 18.2%, 0.51, −39% | **37.0%, 1.16, −26%** |
| S, the composite's top 20, no engine | 12.3%, 0.37, −38% | 19.0%, 0.73, −19% |
| The universe by market cap | 10.3%, 0.30, −32% | 12.4%, 0.45, −20% |
| The universe, equal weights | 4.9%, 0.10, −55% | 20.0%, 0.74, −24% |

**The engine didn't beat the Rankings order.**
- **Development:** E2 had the better Sharpe, 0.64 against 0.51, though not significantly.
- **Confirmation:** M made 37.0% a year against E2's 24.4%, with the better Sharpe (1.16
  against 0.95). Memmel's z is −0.95; passing needed +1.65.
- **What the engine does:** it spreads the bet over about 55 stocks and holds risk near
  the benchmark. That lowered volatility by a quarter (18.7% against 25.1%) and the worst
  fall a little, and cut trading costs by two thirds.
- **What it gives up:** in a strong period for momentum it gave up more return than it
  saved in risk.

**BlackRock's composite was weaker than momentum alone, in both periods.**
- Adding value, quality, growth and low volatility to momentum diluted it, as
  `research/ml_rating` and `research/ai_tuning` also found.
- Its top 20 (S) and its engine (E1) both trailed the momentum versions.

**The engine still beat the market.**
- E2 beat the universe by market cap by 10.7% a year in confirmation (t 2.86), with a
  9% tracking error.
- That gain comes from the momentum score; the Rankings order gets more of it by holding
  only 20 stocks.

## Against the app's goals

- **Investing:**
  - for the most return, the concentrated Rankings order stays the better tested way to
    pick;
  - for a smoother ride, about 55 stocks with risk held near the market's, E2's approach
    lost less in bad spells, but not enough to make up for the lower return here.
- **Trading:** this is a monthly portfolio engine, not a source of short-term calls.

## Limits

- **Industries:** they come from today's NSE lists. Stocks no longer listed are
  "Unclassified": 43% of the universe in 2015 and 7% in 2025. That grouping knows which
  stocks were later delisted; it affects the risk model only, not the scores or returns.
- **Market caps:** 192 infinite market caps and others above Rs 30 lakh crore (unit errors
  in share counts) count as missing.
- **Momentum's own history:** momentum was chosen as the Rankings order on these same
  years, so M is favoured in both periods.
- **Liquidity:** limits and BlackRock's own signals and parameters could not be
  reproduced.

## Files

- `engine.py`: the engine and the comparisons. Output in `dev.txt` and `holdout.txt`.
- `analyze.py`: the summary and Memmel's test.

They ran with `uv run --no-project --with cvxpy --with polars --with numpy python -I` on
the scratch data of `research/ml_rating` and `research/promoter`, which is not kept in the
repository.

## Sources

- [JioBlackRock Balanced Advantage Fund, as reported by NiftyTrader](https://www.niftytrader.in/markets/?p=31143)
- [JioBlackRock Flexi Cap Fund and Aladdin, Business Today](https://www.businesstoday.in/mutual-funds/story/jioblackrock-flexi-cap-fund-sip-lump-sum-aladdin-technology-496579-2025-10-02)
- [Aladdin Risk](https://www.blackrock.com/aladdin/products/aladdin-risk)
- [BlackRock Scenario Tester: about the analysis](https://www.blackrock.com/us/financial-professionals/tools/scenario-tester-about-the-analysis)
- [REA (Reverse Engineer Anything), coverage](https://www.kucoin.com/news/flash/github-project-rea-surpasses-80k-stars-in-three-days-sparks-open-source-debate)
