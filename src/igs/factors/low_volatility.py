"""Low-volatility pillar. In Indian long-only tests low-volatility portfolios earned
among the highest risk-adjusted excess returns after costs (Raju & Teli 2022, BSE 200,
2007-2021), and NSE's multi-factor indices give it equal weight with momentum, quality
and value."""

from __future__ import annotations

import polars as pl

from igs.factors import base as b
from igs.factors.momentum import MIN_VOL_RETURNS, VOL_SESSIONS, volatility
from igs.factors.registry import factor
from igs.pit.view import PitView


@factor("volatility_1y", "low_volatility", False,
        f"annualised standard deviation of daily log returns over the last {VOL_SESSIONS} "
        f"sessions (needs {MIN_VOL_RETURNS})")
def volatility_1y(view: PitView) -> pl.DataFrame:
    return b.finish(volatility(view), "vol", ["sd", "n_ret"], None, universe=b.companies(view))
