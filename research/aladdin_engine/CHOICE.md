# The choice, made on 2016–2020 only

Written before the confirmation period was run (`dev.txt`).

## Development results

Net of costs; Sharpe over cash at 6% a year.

| | A year | Volatility | Sharpe | Worst fall | Holdings | Turnover a year |
|---|---|---|---|---|---|---|
| E1, composite score, λ 10 / 25 / 60 | 12.2% / 12.4% / 11.5% | 18–19% | 0.39 / 0.41 / 0.36 | −29 to −30% | 45–59 | 0.6–0.9 |
| **E2, momentum score, λ 10** | **19.5%** | **23.1%** | **0.64** | **−30%** | **53** | **1.7** |
| E2, λ 25 / 60 | 16.1% / 13.5% | 20% / 19% | 0.55 / 0.45 | −27% / −26% | 61 / 56 | 1.4 / 1.2 |
| M, the Rankings order (momentum top 20) | 18.2% | 30.3% | 0.51 | −39% | 20 | 5.1 |
| S, the composite's top 20 | 12.3% | 21.1% | 0.37 | −38% | 20 | 3.1 |
| The universe by market cap | 10.3% | 19.4% | 0.30 | −32% | | |
| The universe, equal weights | 4.9% | 27.4% | 0.10 | −55% | | |

**Candidate: E2 at λ = 10**, the highest development Sharpe.
- **What it is:** the engine's risk model, optimizer, costs and limits, with momentum as
  the score.
- **Against M:**
  - higher Sharpe (0.64 against 0.51), with about a quarter less volatility and a smaller
    worst fall;
  - a third of M's trading costs (3.3% against 9.9% over the five years);
  - the two moved together (correlation 0.91), and the Sharpe gap is not significant here
    (Memmel z 0.67).
  - The confirmation period has to show it.

**BlackRock's composite (E1) was weaker than momentum alone.**
- Adding value, quality, growth and low volatility to momentum diluted it. That matches
  `research/ml_rating` and `research/ai_tuning`.
- S, the composite's top 20 without the engine, was weaker still.

**Data note:**
- Market caps above Rs 30 lakh crore, or infinite (unit errors in share counts, 192 of
  them infinite), count as missing. This affects the benchmark weights and the size
  exposure only.
- No solve failed.

Confirmation to run once:

```
engine.py ... holdout   then   analyze.py holdout.parquet E2_10
```
