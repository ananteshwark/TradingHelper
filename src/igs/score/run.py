"""Production scoring for one as-of date.

Order of operations (each step is reported, nothing is silently skipped):
  1. require the look-ahead gate for the current code;
  2. build the point-in-time universe;
  3. compute every enabled factor, minus factors the latest backtest marked
     DROP (respect_ic_status);
  4. set aside implausible factor values (plausibility bounds; never clipped or
     replaced, always logged);
  5. normalise within peers and compute the composite;
  6. evaluate red flags (reject) and cautions;
  7. robustness gates for top-ranked names: weight stability, persistence,
     breadth, concentration;
  8. run-level checks (freshness, drift; see health.py): a run that fails them
     publishes no High conviction names;
  9. assign tiers with every reason, then attach contributions, source filings and
     the plain-language explanation.
Steps 2-9 are `evaluate_date`, which the backtest also runs at every rebalance, so
the failure rates it measures are for exactly these rules.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

import igs.factors  # noqa: F401  (registers factors)
from igs.config import RedFlagsConfig, ScoringConfig, UniverseConfig
from igs.dq import DQLog
from igs.factors.registry import REGISTRY
from igs.pit.gate import require_gate
from igs.pit.view import PitDataset, PitView
from igs.score import explain, robustness, sanity
from igs.score.normalize import ScoreResult, composite, factor_long, normalise
from igs.score.red_flags import LABELS, evaluate
from igs.timeutil import end_of_day_ist
from igs.universe import build_universe

HIGH, WATCH, NOT_SHORTLISTED, REJECTED = ("High conviction", "Watchlist", "Not shortlisted",
                                          "Rejected")

# (view, included universe, composite result) -> run-level problems; any problem
# withholds High conviction for every stock in the run.
RunCheck = Callable[[PitView, pl.DataFrame, ScoreResult], list[str]]


@dataclass
class ScoreRun:
    as_of: dt.datetime
    universe: pl.DataFrame
    results: pl.DataFrame
    pillars: pl.DataFrame
    factors: pl.DataFrame
    flags: pl.DataFrame
    dropped_factors: list[str]
    ic_status_generated_at: str | None
    gate_fingerprint: str
    dq: DQLog = field(default_factory=DQLog)
    robustness: pl.DataFrame = field(default_factory=pl.DataFrame)
    implausible: pl.DataFrame = field(default_factory=pl.DataFrame)
    run_issues: list[str] = field(default_factory=list)


@dataclass
class DateEval:
    as_of: dt.datetime
    view: PitView
    universe: pl.DataFrame
    norm: pl.DataFrame
    res: ScoreResult
    flags: pl.DataFrame
    robustness: pl.DataFrame
    implausible: pl.DataFrame
    results: pl.DataFrame
    run_issues: list[str]


def trading_days(dataset: PitDataset) -> list[dt.date]:
    src = dataset.tables.get("index_prices")
    if src is None or src.height == 0:
        src = dataset.tables.get("prices")
    if src is None or src.height == 0:
        return []
    return sorted(src["trade_date"].unique().to_list())


def composite_at(dataset: PitDataset, as_of: dt.datetime, sc: ScoringConfig,
                 uc: UniverseConfig, dropped: set[str], dq: DQLog | None = None
                 ) -> tuple[PitView, pl.DataFrame, pl.DataFrame, ScoreResult, pl.DataFrame]:
    """Universe, factors (with plausibility applied), normalisation and composite."""
    view = PitView(dataset, as_of)
    universe = build_universe(view, uc)
    inc = universe.filter(pl.col("included"))
    enabled = [f for p in sc.pillars.values() for f in p.enabled if f not in dropped]
    outputs = {f: REGISTRY[f].fn(view).join(inc.select("company_id"), on="company_id")
               for f in enabled}
    outputs, implausible = sanity.apply(outputs, sc.plausibility, dq)
    norm = normalise(factor_long(outputs), inc, sc)
    res = composite(norm, sc, dropped)
    return view, universe, norm, res, implausible


def composite_ranks(res: ScoreResult) -> pl.DataFrame:
    c = res.composite.filter(pl.col("composite").is_not_null())
    return c.select("company_id", (pl.col("composite").rank("ordinal", descending=True)
                                   / pl.len()).alias("rank_pct"))


def rank_history(dataset: PitDataset, sc: ScoringConfig, uc: UniverseConfig,
                 dropped: set[str], cutoff: dt.time | None = None) -> robustness.RankHistory:
    def rank_at(d: dt.date) -> pl.DataFrame:
        as_of = end_of_day_ist(d, cutoff) if cutoff else end_of_day_ist(d)
        return composite_ranks(composite_at(dataset, as_of, sc, uc, dropped)[3])
    return robustness.RankHistory(rank_at)


def evaluate_date(dataset: PitDataset, as_of: dt.datetime, sc: ScoringConfig,
                  uc: UniverseConfig, rf: RedFlagsConfig, dropped: set[str],
                  history: robustness.RankHistory, days: list[dt.date],
                  dq: DQLog | None = None, run_check: RunCheck | None = None) -> DateEval:
    view, universe, norm, res, implausible = composite_at(dataset, as_of, sc, uc, dropped, dq)
    inc = universe.filter(pl.col("included"))
    flags = evaluate(view, inc["company_id"].to_list(), rf)
    history.put(view.as_of_date, composite_ranks(res))
    rejected = set(flags.filter((pl.col("status") == "tripped")
                                & (pl.col("severity") == "reject"))["company_id"].to_list())
    eligible = [c for c in res.composite.filter(pl.col("composite").is_not_null())
                ["company_id"].to_list() if c not in rejected]
    rb = sc.robustness
    hist_dates = robustness.prior_month_ends(days, view.as_of_date, rb.persistence_months) \
        if rb.enabled else []
    ranks = history.ensure(hist_dates)
    rob = robustness.evaluate(res.pillars, res.factors, eligible, dict(sc.pillar_weights),
                              sc.tiers.high_conviction_top_pct, rb, ranks, hist_dates)
    run_issues = run_check(view, inc, res) if run_check else []
    blk = [robustness.blockers(rob, rb), sanity.blockers(implausible)]
    if run_issues:
        blk.append(inc.select("company_id", pl.lit("run held: " + "; ".join(run_issues))
                              .alias("reason")))
    results = (inc.join(res.composite, on="company_id", how="left")
                  .join(rob, on="company_id", how="left"))
    results = assign_tiers(results, flags, sc, pl.concat(blk))
    return DateEval(as_of=as_of, view=view, universe=universe, norm=norm, res=res, flags=flags,
                    robustness=rob, implausible=implausible, results=results,
                    run_issues=run_issues)


def load_ic_status(path: Path | None, as_of: dt.datetime | None = None
                   ) -> tuple[set[str], str | None]:
    if path is None or not path.exists():
        return set(), None
    from igs.provenance import validation_fingerprint
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("validation_fingerprint") != validation_fingerprint():
        return set(), None
    trained = data.get("trained_through")
    if as_of is not None and (not trained or dt.date.fromisoformat(trained) > as_of.date()):
        return set(), None
    dropped = {r["factor"] for r in data.get("factors", []) if r.get("verdict") == "DROP"}
    return dropped, data.get("generated_at")


def flag_blockers(flags: pl.DataFrame) -> pl.DataFrame:
    """Reasons each company cannot be High conviction because of its checks: a tripped
    caution, or a check that could not be evaluated and is configured to block."""
    if flags.height == 0:
        return pl.DataFrame(schema={"company_id": pl.Int64, "reason": pl.Utf8})
    lab = pl.col("flag").replace_strict(LABELS, default=pl.col("flag"))
    caution = flags.filter((pl.col("status") == "tripped") & (pl.col("severity") == "caution"))
    unchecked = flags.filter((pl.col("status") == "data_unavailable")
                             & pl.col("unavailable_blocks"))
    return pl.concat([caution.select("company_id", pl.format("caution: {}", lab).alias("reason")),
                      unchecked.select("company_id",
                                       pl.format("not checked: {}", lab).alias("reason"))])


def assign_tiers(results: pl.DataFrame, flags: pl.DataFrame, sc: ScoringConfig,
                 blockers: pl.DataFrame | None = None) -> pl.DataFrame:
    """Rejected: a tripped reject-severity check, or no composite. Among the rest, ranked
    by composite: High conviction needs the top band, enough coverage and no blocker
    (caution tripped, blocking check not evaluable, robustness gate failed, run health);
    Watchlist is the wider band; everything else is Not shortlisted. Every blocker of a
    top-band name is listed in tier_reason and hc_blockers."""
    t = sc.tiers
    lab = pl.col("flag").replace_strict(LABELS, default=pl.col("flag"))
    rejected = (flags.filter((pl.col("status") == "tripped") & (pl.col("severity") == "reject"))
                     .group_by("company_id").agg(lab.sort().str.join(", ").alias("reject_flags")))
    b_all = flag_blockers(flags)
    if blockers is not None and blockers.height:
        b_all = pl.concat([b_all, blockers.select("company_id", "reason")])
    blocked = b_all.group_by("company_id").agg(pl.col("reason").unique(maintain_order=True)
                                               .alias("_blockers"))
    r = (results.join(rejected, on="company_id", how="left")
                .join(blocked, on="company_id", how="left"))
    eligible = pl.col("reject_flags").is_null() & pl.col("composite").is_not_null()
    r = r.with_columns(pl.when(eligible).then(pl.col("composite")).alias("_c"))
    r = r.with_columns(pl.col("_c").rank("ordinal", descending=True).alias("rank"),
                       pl.col("_c").count().alias("scored"))
    r = r.with_columns((pl.col("rank") / pl.col("scored")).alias("rank_pct"))
    in_band = pl.col("rank_pct") <= t.high_conviction_top_pct / 100
    low_cov = pl.col("coverage") < t.high_conviction_min_coverage
    cov_reason = pl.format("factor coverage {} below {}",
                           (pl.col("coverage").fill_null(0) * 100).round(0).cast(pl.Int64)
                           .cast(pl.Utf8) + "%", pl.lit(f"{t.high_conviction_min_coverage:.0%}"))
    r = r.with_columns(pl.when(in_band).then(
        pl.concat_list(pl.col("_blockers").fill_null(pl.lit([], dtype=pl.List(pl.Utf8))),
                       pl.when(low_cov).then(pl.concat_list(cov_reason))
                         .otherwise(pl.lit([], dtype=pl.List(pl.Utf8)))))
        .otherwise(pl.lit([], dtype=pl.List(pl.Utf8))).alias("hc_blockers"))
    r = r.with_columns(
        pl.when(pl.col("reject_flags").is_not_null()).then(pl.lit(REJECTED))
          .when(pl.col("composite").is_null()).then(pl.lit(REJECTED))
          .when(in_band & (pl.col("hc_blockers").list.len() == 0)).then(pl.lit(HIGH))
          .when(pl.col("rank_pct") <= t.watchlist_top_pct / 100).then(pl.lit(WATCH))
          .otherwise(pl.lit(NOT_SHORTLISTED)).alias("tier"),
        pl.when(pl.col("reject_flags").is_not_null())
          .then(pl.format("red flag: {}", pl.col("reject_flags")))
          .when(pl.col("composite").is_null())
          .then(pl.format("insufficient data (coverage {})",
                          (pl.col("coverage").fill_null(0) * 100).round(0).cast(pl.Int64)
                          .cast(pl.Utf8) + "%"))
          .when(in_band & (pl.col("hc_blockers").list.len() > 0))
          .then(pl.format("top {} by score, but {}", pl.lit(f"{t.high_conviction_top_pct:g}%"),
                          pl.col("hc_blockers").list.join("; ")))
          .otherwise(None).alias("tier_reason"))
    return r.drop("_c", "_blockers")


def score(dataset: PitDataset, as_of: dt.datetime, sc: ScoringConfig, uc: UniverseConfig,
          rf: RedFlagsConfig, ic_status_path: Path | None = None,
          check_gate: bool = True, run_check: RunCheck | None = None) -> ScoreRun:
    gate = require_gate().fingerprint if check_gate else "not checked"
    dq = DQLog()
    dropped: set[str] = set()
    ic_generated = None
    if sc.respect_ic_status:
        dropped, ic_generated = load_ic_status(ic_status_path, as_of)
        if ic_generated is None:
            dq.emit("warn", "factors_unvalidated",
                    "no backtest IC status found: factors are used without IC validation")
        elif dropped:
            dq.emit("info", "factors_dropped_by_ic", f"{len(dropped)} factors dropped by the "
                    f"backtest IC gate: {sorted(dropped)}")
    history = rank_history(dataset, sc, uc, dropped)
    ev = evaluate_date(dataset, as_of, sc, uc, rf, dropped, history, trading_days(dataset), dq,
                       run_check)
    ev.view.audit.assert_clean()
    for issue in ev.run_issues:
        dq.emit("error", "run_health", f"High conviction withheld: {issue}")

    # Source filings for every fact behind every factor value.
    view = ev.view
    facts = view.facts().select("fact_id", "filing_id") if view.has("facts") else \
        pl.DataFrame(schema={"fact_id": pl.Int64, "filing_id": pl.Int64})
    f2f = dict(facts.iter_rows())
    factors = ev.res.factors.with_columns(
        pl.col("source_fact_ids").map_elements(
            lambda ids: sorted({f2f[i] for i in ids if i in f2f}),
            return_dtype=pl.List(pl.Int64)).alias("source_filing_ids"))
    return ScoreRun(as_of=as_of, universe=ev.universe, results=ev.results,
                    pillars=ev.res.pillars, factors=factors, flags=ev.flags,
                    dropped_factors=sorted(dropped), ic_status_generated_at=ic_generated,
                    gate_fingerprint=gate, dq=dq, robustness=ev.robustness,
                    implausible=ev.implausible, run_issues=ev.run_issues)


def explanations(run: ScoreRun, filings: pl.DataFrame,
                 names: dict[int, str] | None = None) -> dict[int, str]:
    labels = explain.filing_labels(filings)
    out = {}
    for r in run.results.iter_rows(named=True):
        company = {**r, "name": (names or {}).get(r["company_id"]) or r.get("symbol")}
        out[r["company_id"]] = explain.why(company, run.factors, run.flags, labels)
    return out
