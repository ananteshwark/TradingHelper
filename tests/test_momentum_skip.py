"""Skip-month momentum: the return ends 21 sessions before the latest close."""

from __future__ import annotations

import pytest
from test_factors import AS_OF, _dataset

from igs.config import load_scoring
from igs.factors import momentum
from igs.pit import PitView


def test_skip_month_return_ends_a_month_ago():
    view = PitView(_dataset(), AS_OF)
    latest = momentum._returns(view, 252).filter(momentum.pl.col("company_id") == 1).row(
        0, named=True)
    skipped = momentum._returns(view, 252, 21).filter(
        momentum.pl.col("company_id") == 1).row(0, named=True)
    # Company 1 rises 0.1% a session over the last 300 sessions (adjusted for its split).
    assert latest["stock_ret"] == pytest.approx(1.001 ** 252 - 1)
    assert skipped["stock_ret"] == pytest.approx(1.001 ** (252 - 21) - 1)
    assert skipped["d_start"] == latest["d_start"]
    assert (latest["d_end"] - skipped["d_end"]).days >= 21     # 21 sessions, plus weekends


def test_skip_month_versions_are_tracked_not_scored():
    weights = load_scoring().pillars["momentum"].factor_weights()
    assert weights["risk_adj_return_6m_skip1m"] == weights["risk_adj_return_12m_skip1m"] == 0
    assert weights["risk_adj_return_6m"] == weights["risk_adj_return_12m"] == 0.5
