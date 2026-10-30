# Study C choice (written before the holdout was computed)

2026-10-25

Development 2023-2024, eight candidates (`dev.txt`). Highest t-statistic of daily net P&L:
**hybrid, k = 3, no gate**: 2,939 trades, +0.150% gross and +0.027% net a trade, profit
factor 1.04, Rs 165 a day, t 0.57 (2023: -Rs 196 a day; 2024: +Rs 524 a day).

Daily rank IC of the predictions in development: seq 0.013 (t 3.0), hybrid 0.026 (t 4.7),
LightGBM baseline 0.034 (t 6.2). The baseline (LightGBM, k = 3, gate) made Rs 368 a day
(t 1.54) on the same rows: every sequence candidate trailed it in development.

Holdout to be computed once: `seq_model.py evaluate PRED holdout hybrid 3 nogate`.
