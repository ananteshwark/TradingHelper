# Which intraday calls work

`PROTOCOL.md` (fingerprinted and committed before any outcome), `replay.py` (the current
rules, `intraday-v4`, replayed on 2022-2026 candles with each call's inputs and result),
`analyze.py` (Part 1, the walk-forward predictions, the six candidates, the holdout).
Outputs: `describe.txt` (Part 1, 2022-2024), `dev.txt` (the candidates, 2023-2024),
`CHOICE.md`, `holdout.txt` (the one holdout run, 2025 to 8 October 2026). Write-up:
`docs/INTRADAY_FILTER.md`.

Data: `research/ml`'s Upstox 5-minute candles (`fetch_candles.py`): 202 Nifty 200 stocks and
the Nifty 50, end of January 2022 to 8 October 2026. Not kept in the repository.

A share priced above the Rs 1 lakh position can't be traded at that size; such calls get
no result (as in `research/ml`, which skipped them). The first run stopped on one with a
division by zero; `igs.intraday.backtest.replay` had the same fault and was fixed with it.
