# Study B choice (written before the holdout was computed)

2026-10-09T10:01:55Z

Development 2023-2024, eight candidates (intraday_dev.txt). Highest t-statistic of daily
net P&L: LightGBM, k = 3, gate on (predicted move at least 0.15%):
2,045 trades, +0.179% gross and +0.056% net a trade, profit factor 1.07, Rs 232 a day,
t 1.06 (2023: -Rs 66 a day; 2024: +Rs 529 a day).
Daily rank IC of the predictions in development: LightGBM 0.034 (t 6.0), ridge 0.020 (t 3.5).

Holdout to be computed once: intraday_model.py evaluate PRED holdout gbm 3 gate.
