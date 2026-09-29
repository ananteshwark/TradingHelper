"""Run the configured backtests from the database."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from igs.backtest.engine import BacktestResult, run_backtest
from igs.backtest.report import write_ic_status, write_report
from igs.config import load_backtest, load_costs, load_scoring, load_universe
from igs.pit.gate import require_gate
from igs.pit.loader import load_dataset
from igs.universe import price_series

HISTORY_YEARS = 6          # 5-year lookbacks plus a year of slack
FORWARD_MONTHS = 13


def run_configured(conn, start: dt.date, end: dt.date, reports_dir: Path,
                   ic_status_path: Path) -> dict[str, tuple[BacktestResult, Path]]:
    require_gate()   # the backtest scores with production code: same gate applies
    bt, sc, uc, cc = load_backtest(), load_scoring(), load_universe(), load_costs()
    dataset = load_dataset(conn, dt.date(start.year - HISTORY_YEARS, start.month, 1),
                           end + dt.timedelta(days=31 * FORWARD_MONTHS),
                           series=tuple(price_series(uc)))
    out = {}
    for freq in [bt.rebalance.primary, *bt.rebalance.sensitivity]:
        res = run_backtest(dataset, start, end, freq, bt, sc, uc, cc)
        path = write_report(res, reports_dir / f"backtest_{freq}_{start}_{end}",
                            f"Backtest {freq} {start} to {end}")
        if freq == bt.rebalance.primary:
            write_ic_status(res, ic_status_path)
        out[freq] = (res, path)
    return out
