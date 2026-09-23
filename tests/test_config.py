from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from igs.config import (
    ScoringConfig,
    config_dir,
    load_backtest,
    load_red_flags,
    load_scoring,
    load_universe,
)


def test_agreed_defaults():
    u = load_universe()
    assert u.min_market_cap_cr == 500 and u.min_filing_quarters == 8
    assert not u.include_sme and not u.include_asm and not u.include_gsm
    s = load_scoring()
    assert s.pillar_weights == {"growth": 0.35, "quality": 0.25, "valuation": 0.15,
                                "momentum": 0.15, "ownership": 0.10}
    assert s.peer_group.level == "industry"
    b = load_backtest()
    assert b.rebalance.primary == "monthly" and b.rebalance.sensitivity == ["quarterly"]
    assert b.forward_horizons_months == [3, 6, 12]
    rf = load_red_flags()
    assert rf.flag("pledge")["max_pledged_pct_of_promoter"] == 20.0


def test_equal_within_pillar_weights():
    w = load_scoring().pillars["growth"].factor_weights()
    assert sum(w.values()) == pytest.approx(1.0)
    assert len(set(w.values())) == 1


def _scoring_raw() -> dict:
    return yaml.safe_load((config_dir() / "scoring.yaml").read_text())


def test_weights_must_sum_to_one():
    raw = _scoring_raw()
    raw["pillar_weights"]["growth"] = 0.5
    with pytest.raises(ValidationError, match="sum to"):
        ScoringConfig.model_validate(raw)


@pytest.mark.parametrize("module", ["bank", "nbfc"])
def test_ev_ebitda_never_for_financials(module):
    raw = copy.deepcopy(_scoring_raw())
    raw["valuation_modules"][module].append("ev_ebitda")
    with pytest.raises(ValidationError, match="EV/EBITDA"):
        ScoringConfig.model_validate(raw)


def test_unknown_keys_rejected():
    raw = _scoring_raw()
    raw["pilar_weights"] = raw.pop("pillar_weights")   # typo
    with pytest.raises(ValidationError):
        ScoringConfig.model_validate(raw)
