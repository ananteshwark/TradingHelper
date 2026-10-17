# Choices (written before either holdout was computed)

2026-10-09T11:55:45Z

R1 (r1_dev.txt, 2022-2024): every candidate lost after costs. Highest t-statistic: fade
moves of at least 1% on the session after results, -0.053% a trade, t -0.67, 775 trades.
Following the move did worst (-0.19%, t -2.45). Its holdout is run as the protocol says,
though a candidate that lost in development is not expected to pass.

R2 (r2_dev.txt, 2013-2020): highest t-statistic: buy after a reaction of +5% or more, hold
21 sessions: +1.02% net excess over the universe an event, t 2.24 (monthly), positive in
6 of 8 years, 938 events. Reactions of -5% or worse drifted down: -1.04% over 5 sessions
and -1.58% over 63, before costs.

Holdouts to run once: intraday_b1.py report ... holdout fade 0.01; drift.py ... holdout 0.05 21.
