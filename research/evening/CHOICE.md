# Study D choice (written before the holdout was computed)

2026-10-25

Development 2023-2024 at 0.05% slippage a side, eight candidates (`dev.txt`). Highest
t-statistic of daily net P&L: **LightGBM (five seeds averaged), k = 3, gate on**: 2,083
trades, +0.259% gross and +0.077% net a trade, profit factor 1.08, Rs 320 a day, t 1.13
(2023: +Rs 171 a day; 2024: +Rs 469 a day). Long trades -0.14% net, short sells +0.21%.

Daily rank IC in development: ridge 0.025 (t 4.5), LightGBM 0.056 (t 10.5). Every ridge
candidate lost money after costs.

Holdout to be computed once: `model.py evaluate PRED holdout gbm 3 gate`.
