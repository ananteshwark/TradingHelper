"""Look-ahead tests. These gate the scoring layer (`igs gate run`).

They prove two things:
  1. the point-in-time view only ever returns what was public at as_of
     (filed_at, not period_end; restatements only after they were filed;
     corporate actions only from their ex-date);
  2. the harness catches computations that leak, so a registered factor that
     passes it is genuinely point-in-time.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib
import pkgutil
from pathlib import Path

import polars as pl
import pytest
from synthetic import AS_OF_DATES, facts_frame, ist, standard_dataset

import igs.factors
from igs.factors.registry import REGISTRY
from igs.normalize.adjust import adjusted_prices, event_factors
from igs.pit import LookAheadError, PitView, facts_as_of
from igs.pit.harness import check_no_lookahead
from igs.pit.view import AccessAudit

pytestmark = pytest.mark.lookahead


def _revenue(view: PitView, company_id: int, period_end: dt.date) -> float | None:
    df = view.facts(concepts=["revenue"], company_ids=[company_id]).filter(
        pl.col("period_end") == period_end)
    return df["value"][0] if df.height else None


# --------------------------------------------------------------------------- view semantics


def test_filters_on_filed_at_not_period_end():
    ds = standard_dataset()
    q2 = dt.date(2023, 9, 30)
    # Period ended weeks earlier, but results were disseminated at 18:30 IST.
    assert _revenue(PitView(ds, ist(2023, 11, 14, 15, 30)), 1, q2) is None
    assert _revenue(PitView(ds, ist(2023, 11, 14, 23, 59)), 1, q2) == 120


def test_restatement_only_visible_after_it_was_filed():
    ds = standard_dataset()
    q1 = dt.date(2023, 6, 30)
    assert _revenue(PitView(ds, ist(2024, 2, 1, 23, 59)), 1, q1) == 100
    assert _revenue(PitView(ds, ist(2024, 2, 12, 23, 59)), 1, q1) == 90


def test_restatement_is_a_new_row_not_an_overwrite():
    facts = standard_dataset().tables["facts"]
    q1 = facts.filter((pl.col("company_id") == 1) & (pl.col("period_end") == dt.date(2023, 6, 30)))
    assert sorted(q1["value"].to_list()) == [90.0, 100.0]


def test_latest_version_is_chosen_by_filed_at_not_ingestion_order():
    # Backfills can ingest a restatement before the original; fact_id order must not matter.
    facts = facts_frame([
        {"fact_id": 1, "company_id": 1, "period_end": dt.date(2023, 6, 30), "concept": "x",
         "value": 90, "filed_at": ist(2024, 2, 12, 17)},
        {"fact_id": 2, "company_id": 1, "period_end": dt.date(2023, 6, 30), "concept": "x",
         "value": 100, "filed_at": ist(2023, 8, 10, 16)},
    ])
    assert facts_as_of(facts, ist(2023, 12, 1))["value"].to_list() == [100.0]
    assert facts_as_of(facts, ist(2024, 3, 1))["value"].to_list() == [90.0]


def test_filed_at_boundary_is_inclusive():
    facts = facts_frame([{"company_id": 1, "period_end": dt.date(2023, 6, 30), "concept": "x",
                          "value": 1, "filed_at": ist(2023, 8, 10, 16)}])
    assert facts_as_of(facts, ist(2023, 8, 10, 16)).height == 1
    assert facts_as_of(facts, ist(2023, 8, 10, 16) - dt.timedelta(microseconds=1)).height == 0


def test_naive_as_of_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        PitView(standard_dataset(), dt.datetime(2024, 1, 1))  # noqa: DTZ001


def test_prices_stop_at_as_of_and_adjust_only_after_ex_date():
    ds = standard_dataset()
    # Bonus announced 2024-02-20, ex 2024-03-15: on 2024-03-01 no adjustment yet.
    before = PitView(ds, ist(2024, 3, 1, 23, 59)).prices([2])
    assert before["trade_date"].max() == dt.date(2024, 3, 1)
    assert (before["cum_factor"] == 1.0).all()
    after = PitView(ds, ist(2024, 4, 1, 23, 59)).prices([2])
    pre_ex = after.filter(pl.col("trade_date") < dt.date(2024, 3, 15))
    assert (pre_ex["cum_factor"] == 0.5).all()


def test_audit_raises_on_future_rows():
    audit = AccessAudit(ist(2024, 1, 1))
    audit.record("facts", pl.DataFrame({"known_at": [ist(2024, 1, 2)]}))
    with pytest.raises(LookAheadError):
        audit.assert_clean()


# --------------------------------------------------------------------------- harness catches leaks


def clean_latest_revenue(view: PitView) -> pl.DataFrame:
    f = view.facts(concepts=["revenue"])
    return (f.sort("period_end").group_by("company_id").agg(pl.col("value").last())
             .sort("company_id"))


def clean_20d_return(view: PitView) -> pl.DataFrame:
    px = view.prices().sort("security_id", "trade_date")
    return (px.group_by("security_id")
              .agg((pl.col("adj_close").last() / pl.col("adj_close").tail(21).first() - 1)
                   .alias("ret_20d"))
              .sort("security_id"))


def clean_last_adjusted_close(view: PitView) -> pl.DataFrame:
    return (view.prices().sort("trade_date").group_by("security_id")
                .agg(pl.col("adj_close").last()).sort("security_id"))


def leaky_period_end_filter(view: PitView) -> pl.DataFrame:
    # The classic mistake: "periods that ended before as_of" instead of "filed before as_of".
    f = view._data.tables["facts"].filter(pl.col("period_end") <= view.as_of_date)
    return (f.sort("period_end", "filed_at").group_by("company_id").agg(pl.col("value").last())
             .sort("company_id"))


def leaky_future_adjustments(view: PitView) -> pl.DataFrame:
    # Back-adjusts with every corporate action on file, including ones not yet announced.
    px = view.table("prices")
    factors = event_factors(view._data.tables["corporate_actions"], px)
    adj = adjusted_prices(px, factors, dt.date.max)
    return (adj.sort("trade_date").group_by("security_id").agg(pl.col("adj_close").last())
               .sort("security_id"))


def leaky_latest_price(view: PitView) -> pl.DataFrame:
    px = view._data.tables["prices"]
    return px.sort("trade_date").group_by("security_id").agg(pl.col("close").last()).sort(
        "security_id")


@pytest.mark.parametrize("fn", [clean_latest_revenue, clean_20d_return, clean_last_adjusted_close])
def test_harness_passes_clean_computations(fn):
    check_no_lookahead(fn, standard_dataset(), AS_OF_DATES, name=fn.__name__)


@pytest.mark.parametrize("fn", [leaky_period_end_filter, leaky_future_adjustments,
                                leaky_latest_price])
def test_harness_catches_leaks(fn):
    with pytest.raises(LookAheadError):
        check_no_lookahead(fn, standard_dataset(), AS_OF_DATES, name=fn.__name__)


# --------------------------------------------------------------------------- the gate proper


def _import_all_factor_modules() -> None:
    for mod in pkgutil.walk_packages(igs.factors.__path__, prefix="igs.factors."):
        importlib.import_module(mod.name)


@pytest.fixture(scope="module")
def market():
    import synthetic_market
    return synthetic_market.build()


def test_every_enabled_factor_is_registered():
    from igs.config import load_scoring
    _import_all_factor_modules()
    enabled = {f for p in load_scoring().pillars.values() for f in p.enabled}
    assert enabled - set(REGISTRY) == set(), "enabled in scoring.yaml but not implemented"
    for name in enabled:
        pillar = next(p for p, cfg in load_scoring().pillars.items() if name in cfg.enabled)
        assert REGISTRY[name].pillar == pillar, name


@pytest.mark.parametrize("name", sorted(
    __import__("igs.factors", fromlist=["REGISTRY"]).REGISTRY))
def test_every_registered_factor_is_point_in_time(name, market):
    import synthetic_market
    spec = REGISTRY[name]
    check_no_lookahead(spec.fn, market, synthetic_market.GATE_DATES, name=name)
    # The harness is only meaningful if the factor produces values at all.
    last = spec.fn(PitView(market, synthetic_market.GATE_DATES[-1]))
    assert (last["status"] == "ok").sum() >= 3, name


def test_every_check_is_point_in_time(market):
    """Red flags and cautions go through the same harness as factors."""
    import synthetic_market

    from igs.config import load_red_flags
    from igs.score.red_flags import FLAGS, evaluate
    cfg = load_red_flags()
    companies = list(range(1, 7))
    check_no_lookahead(lambda v: evaluate(v, companies, cfg), market,
                       synthetic_market.GATE_DATES, name="checks")
    last = evaluate(PitView(market, synthetic_market.GATE_DATES[-1]), companies, cfg)
    evaluated = set(last.filter(pl.col("status").is_in(["tripped", "clear"]))["flag"])
    # Checks that need data the synthetic market does not have (announcements,
    # surveillance, audit opinions, contingent liabilities, exceptional items) aside,
    # every check must actually evaluate something, or the harness proves nothing.
    missing_inputs = {"auditor_qualification", "resignations", "surveillance",
                      "contingent_liabilities", "exceptional_items"}
    assert set(FLAGS) - missing_inputs <= evaluated, set(FLAGS) - missing_inputs - evaluated


def test_whole_scoring_pipeline_is_point_in_time(market):
    """Universe, factors, plausibility, composite, checks, robustness (including the
    rank history at earlier month-ends) and tiers, end to end through the harness: the
    pipeline re-creates its own views from the (full, truncated or poisoned) dataset."""
    import synthetic_market

    from igs.config import load_red_flags, load_scoring, load_universe
    from igs.score.run import evaluate_date, rank_history, trading_days
    sc = load_scoring().model_copy(update={
        "peer_group": load_scoring().peer_group.model_copy(update={"min_peers": 2}),
        "tiers": load_scoring().tiers.model_copy(update={"high_conviction_top_pct": 40.0,
                                                         "watchlist_top_pct": 60.0})})
    uc = load_universe().model_copy(update={"min_market_cap_cr": 0.0})
    rf = load_red_flags()

    def pipeline(view: PitView) -> pl.DataFrame:
        ds = view._data
        ev = evaluate_date(ds, view.as_of, sc, uc, rf, set(), rank_history(ds, sc, uc, set()),
                           trading_days(ds))
        r = ev.results.with_columns(pl.col("hc_blockers").list.sort().list.join(" | "))
        return r.select(sorted(r.columns))
    check_no_lookahead(pipeline, market, synthetic_market.GATE_DATES, name="scoring pipeline")
    last = pipeline(PitView(market, synthetic_market.GATE_DATES[-1]))
    assert last["composite"].drop_nulls().len() >= 3


FORBIDDEN_IMPORTS = ("igs.db", "igs.ingest", "psycopg", "httpx", "requests", "urllib",
                     "socket", "sqlite3", "yfinance",
                     # a language model knows what happened after the as-of date
                     "igs.assistant", "anthropic")


def test_factor_modules_cannot_bypass_the_view():
    root = Path(igs.factors.__file__).parent
    problems = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.startswith(FORBIDDEN_IMPORTS):
                    problems.append(f"{path.name}: imports {n}")
            if isinstance(node, ast.Attribute) and node.attr == "_data":
                problems.append(f"{path.name}:{node.lineno}: touches PitView._data")
    assert not problems, problems
