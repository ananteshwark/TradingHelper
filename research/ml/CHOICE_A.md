# Study A choice (written before the holdout was computed)

2026-10-09T10:42:52Z

Development 2016-2020, eight candidates and the momentum baseline (daily_dev.txt). Highest
information ratio of monthly net excess return over the universe among the eight:
LightGBM, weekly, N = 20: +0.83% a month after costs (0.82% a month of costs, 49% of the
names changed a week), IR 0.88, t 1.98, positive in 4 of 5 years.

In the same framework the 12-1 momentum baseline did better in development: monthly N = 10
+1.57% a month (IR 1.04), weekly N = 10 +1.42% (IR 0.95).

Holdout to be computed once: daily_model.py evaluate BUILD holdout gbm weekly 20, with the
momentum baseline in the same framework (weekly N = 20), and, for information, the monthly
N = 10 momentum rule the app already tracks on paper.
