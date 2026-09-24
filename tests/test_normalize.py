from __future__ import annotations

import polars as pl
import pytest

import igs.factors  # noqa: F401
from igs.config import load_scoring
from igs.score.normalize import composite, normalise

CFG = load_scoring()


def _long(rows: list[tuple]) -> pl.DataFrame:
    """rows: (company_id, factor, value, status)."""
    from igs.factors.registry import REGISTRY
    return pl.DataFrame([{"company_id": c, "factor": f, "pillar": REGISTRY[f].pillar,
                          "higher_is_better": REGISTRY[f].higher_is_better, "value": v,
                          "status": s} for c, f, v, s in rows],
                        schema_overrides={"value": pl.Float64})


def _peers(n: int, industry=lambda i: "A", sector=lambda i: "S") -> pl.DataFrame:
    return pl.DataFrame({"company_id": list(range(1, n + 1)),
                         "industry": [industry(i) for i in range(1, n + 1)],
                         "sector": [sector(i) for i in range(1, n + 1)]})


def test_z_scores_are_within_industry():
    # Two industries with very different ROCE levels: each company is judged against its own.
    rows = [(i, "roce", (0.30 if i <= 10 else 0.05) + 0.001 * i, "ok") for i in range(1, 21)]
    peers = _peers(20, industry=lambda i: "IT" if i <= 10 else "Steel")
    out = normalise(_long(rows), peers, CFG)
    assert set(out["peer_level"]) == {"industry"}
    by = {r["company_id"]: r for r in out.iter_rows(named=True)}
    # Both are best within their own industry although Steel's best ROCE is far below IT's
    # worst (winsorising is market-wide, z-scoring is within the industry).
    assert by[10]["peer_percentile"] == by[20]["peer_percentile"] == 1.0
    assert by[20]["z"] > 1.4 and by[11]["z"] < -1.4
    assert abs(sum(r["z"] for r in out.iter_rows(named=True) if r["industry"] == "IT")) < 1e-9


def test_thin_industry_falls_back_to_sector_but_never_to_market():
    rows = [(i, "roce", 0.1 * i, "ok") for i in range(1, 13)]
    peers = _peers(12, industry=lambda i: "Tiny" if i <= 3 else "Big",
                   sector=lambda i: "S1" if i <= 3 else "S2")
    out = normalise(_long(rows), peers, CFG)
    tiny = out.filter(pl.col("industry") == "Tiny")
    # Tiny industry (3) and its sector S1 (3) are both below min_peers=8: no z-score.
    assert tiny["z"].null_count() == 3 and set(tiny["status"]) == {"insufficient_peers"}
    big = out.filter(pl.col("industry") == "Big")
    assert set(big["peer_level"]) == {"industry"} and big["z"].null_count() == 0


def test_sector_fallback():
    rows = [(i, "roce", 0.1 * i, "ok") for i in range(1, 11)]
    peers = _peers(10, industry=lambda i: f"I{i % 3}", sector=lambda i: "S")
    out = normalise(_long(rows), peers, CFG)
    assert set(out["peer_level"]) == {"sector"} and set(out["peer_group"]) == {"S"}


def test_winsorisation_and_direction():
    rows = [(i, "net_debt_to_ebitda", float(i), "ok") for i in range(1, 200)]
    rows.append((200, "net_debt_to_ebitda", 1e6, "ok"))       # absurd outlier
    out = normalise(_long(rows), _peers(200), CFG)
    top = out.filter(pl.col("company_id") == 200).row(0, named=True)
    assert top["winsorized"] < 200                           # clipped at the 99th pct
    assert top["z"] < 0                                      # lower is better: flipped
    low = out.filter(pl.col("company_id") == 1).row(0, named=True)
    assert low["z"] > 0


def test_composite_renormalises_over_applicable_factors_and_reports_coverage():
    growth = ["revenue_cagr_3y", "revenue_ttm_yoy"]
    rows = []
    for i in range(1, 11):
        for f in growth:
            rows.append((i, f, float(i), "ok"))
        rows.append((i, "roce", float(i), "ok" if i != 1 else "not_applicable"))
        rows.append((i, "pb", float(i), "ok"))
        rows.append((i, "rs_6m_vs_nifty500", float(i), "ok"))
        rows.append((i, "pledge_pct", float(i), "ok"))
    long = _long(rows)
    res = composite(normalise(long, _peers(10), CFG), CFG)
    comp = {r["company_id"]: r for r in res.composite.iter_rows(named=True)}
    # Every pillar has at least one factor for these companies.
    assert comp[5]["coverage"] == pytest.approx(1.0)
    # Contributions add up to the composite.
    for cid in (2, 7):
        contrib = res.factors.filter(pl.col("company_id") == cid)["contribution"].sum()
        assert contrib == pytest.approx(comp[cid]["composite"])
    # Company 1's quality pillar has only a not-applicable factor -> pillar empty, composite
    # renormalised over the remaining 75% of weight.
    q1 = res.pillars.filter((pl.col("company_id") == 1) & (pl.col("pillar") == "quality"))
    assert q1["score"][0] is None
    assert comp[1]["coverage"] == pytest.approx(0.75)


def test_insufficient_coverage_leaves_composite_empty():
    rows = [(i, "rs_6m_vs_nifty500", float(i), "ok") for i in range(1, 11)]
    res = composite(normalise(_long(rows), _peers(10), CFG), CFG)
    assert res.composite["composite"].null_count() == 10     # 15% of weight is not enough
    assert res.composite["coverage"].to_list() == pytest.approx([0.15] * 10)


def test_dropped_factors_are_excluded():
    rows = [(i, f, float(i), "ok") for i in range(1, 11)
            for f in ("revenue_cagr_3y", "roce", "pb", "rs_6m_vs_nifty500", "pledge_pct")]
    res = composite(normalise(_long(rows), _peers(10), CFG), CFG, dropped={"pb"})
    assert "pb" not in set(res.factors["factor"])
    assert res.composite["coverage"][0] == pytest.approx(0.85)
