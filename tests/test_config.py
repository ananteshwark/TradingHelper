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
    # Momentum alone since Oct 2026 (research/ml_rating; tests/test_momentum_ranking.py).
    assert s.pillar_weights["momentum"] == 1.0
    # Without growth (the pillar that needs the longest history) coverage can still
    # reach the High conviction minimum.
    assert 1 - s.pillar_weights["growth"] >= s.tiers.high_conviction_min_coverage
    assert s.peer_group.level == "industry"
    b = load_backtest()
    assert b.ic_gate.min_abs_t == 3.0
    assert b.rebalance.primary == "monthly" and b.rebalance.sensitivity == ["quarterly"]
    assert b.forward_horizons_months == [3, 6, 12]
    rf = load_red_flags()
    assert rf.flag("pledge")["max_pledged_pct_of_promoter"] == 20.0


def test_growth_weights_preserve_existing_factors_and_track_new_ones():
    w = load_scoring().pillars["growth"].factor_weights()
    assert sum(w.values()) == pytest.approx(1.0)
    shadow = {'revenue_quarter_yoy', 'pat_quarter_yoy', 'revenue_2q_yoy',
              'pat_2q_yoy', 'opm_quarter_yoy', 'eps_diluted_yoy'}
    assert {name for name, weight in w.items() if weight == 0} == shadow
    assert {weight for name, weight in w.items() if name not in shadow} == {0.1}


def test_zero_weight_factors_are_tracked_not_scored():
    s = load_scoring()
    w = s.pillars["momentum"].factor_weights()
    assert w["risk_adj_return_6m"] == w["risk_adj_return_12m"] == 0.5
    assert w["delivery_pct_20d_vs_1y"] == 0.0
    assert s.pillars["ownership"].factor_weights()["fii_dii_holding_change"] == 0.0


def test_negative_factor_weights_rejected():
    raw = copy.deepcopy(_scoring_raw())
    raw["pillars"]["ownership"]["weights"].update(pledge_pct=0.75, fii_dii_holding_change=-0.25)
    with pytest.raises(ValidationError, match="non-negative"):
        ScoringConfig.model_validate(raw)


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


def test_alert_settings_saved_with_whatsapp_still_load():
    """WhatsApp messages were removed on 2026-10-27; alert settings saved before that name
    the channel and its section, and still load, without them."""
    from igs.config import AlertsConfig, load_alerts
    shipped = load_alerts()
    old = shipped.model_dump()
    old["channels"] = {**old["channels"], "whatsapp": True}
    old["whatsapp"] = {"meta_template": "igs_ai_call", "meta_language": "en"}
    loaded = AlertsConfig.model_validate(old)
    assert "whatsapp" not in loaded.channels and loaded == shipped
