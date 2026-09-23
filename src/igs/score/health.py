"""Run health: are this run's inputs fresh, and does it look like the last run?

A ranking built on a half-loaded bhavcopy, a results feed that stopped a month
ago, or a parser that silently lost a column can look perfectly normal. These
run-level checks catch that before anything is called High conviction:

  freshness  latest prices (and the share of the universe that has them),
             announcements, ASM/GSM lists and results filings are recent enough
  drift      compared with the previous run (if recent): universe size, median
             coverage, each factor's share of computable values, and how much of
             the previous top decile is still there

Any problem withholds High conviction for the whole run (shown as the reason),
is logged as a data-quality error, and is stored with the run for the alert
digest and the UI. The run itself is kept for inspection.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from igs.config import RunHealth
from igs.factors import base as b
from igs.pit.view import PitView
from igs.score.normalize import ScoreResult
from igs.timeutil import ist_date


def summary(inc: pl.DataFrame, res: ScoreResult) -> dict:
    comp = res.composite.filter(pl.col("composite").is_not_null())
    n = comp.height
    top = (comp.sort("composite", descending=True).head(max(1, n // 10))["company_id"].to_list()
           if n else [])
    f = res.factors
    ok = (f.group_by("factor").agg((pl.col("status") == "ok").mean().alias("ok"))
          if f.height else pl.DataFrame(schema={"factor": pl.Utf8, "ok": pl.Float64}))
    return {"universe_size": inc.height, "scored": n,
            "median_coverage": res.composite["coverage"].median() if res.composite.height
            else None,
            "factor_ok_share": {r["factor"]: r["ok"] for r in ok.iter_rows(named=True)},
            "top_decile": sorted(top)}


def freshness(view: PitView, inc: pl.DataFrame, cfg: RunHealth) -> list[str]:
    issues = []
    today = view.as_of_date
    px = b.primary_prices(view)
    if px.height == 0:
        return ["no prices loaded"]
    latest = px["trade_date"].max()
    age = (today - latest).days
    if age > cfg.max_price_age_days:
        issues.append(f"latest prices are from {latest} ({age} days before the as-of date)")
    traded = px.filter(pl.col("trade_date") == latest).select("company_id").unique()
    if inc.height:
        share = inc.join(traded, on="company_id").height / inc.height
        if share < cfg.min_universe_traded_share:
            issues.append(f"only {share:.0%} of the universe has a price on {latest} (partial "
                          "price file?)")

    def age_of(table: str, col: str, label: str, limit: int, where: pl.Expr | None = None):
        if not view.has(table):
            issues.append(f"no {label} loaded")
            return
        t = view.table(table)
        if where is not None:
            t = t.filter(where)
        if t.height == 0:
            issues.append(f"no {label} loaded")
            return
        last = t[col].max()
        last_d = ist_date(last) if isinstance(last, dt.datetime) else last
        days = (today - last_d).days
        if days > limit:
            issues.append(f"latest {label} is from {last_d} ({days} days old; limit {limit})")
    age_of("announcements", "filed_at", "announcement", cfg.max_announcement_age_days)
    age_of("surveillance", "effective_from", "ASM/GSM list", cfg.max_surveillance_age_days)
    if view.has("filings"):
        age_of("filings", "filed_at", "results filing", cfg.max_results_age_days,
               pl.col("filing_type") == "financial_results")
    else:
        age_of("facts", "filed_at", "results filing", cfg.max_results_age_days)
    return issues


def drift(cur: dict, prev: dict, cfg: RunHealth) -> list[str]:
    issues = []
    pu, cu = prev.get("universe_size") or 0, cur.get("universe_size") or 0
    if pu and abs(cu - pu) / pu > cfg.max_universe_change:
        issues.append(f"universe size changed from {pu} to {cu} since the previous run")
    pc, cc = prev.get("median_coverage"), cur.get("median_coverage")
    if pc is not None and cc is not None and pc - cc > cfg.max_coverage_drop:
        issues.append(f"median factor coverage fell from {pc:.0%} to {cc:.0%}")
    drops = [f"{k} ({v:.0%} -> {cur['factor_ok_share'].get(k, 0.0):.0%})"
             for k, v in (prev.get("factor_ok_share") or {}).items()
             if v - cur["factor_ok_share"].get(k, 0.0) > cfg.max_factor_ok_drop]
    if drops:
        issues.append("factors lost computable values: " + ", ".join(sorted(drops)))
    pt, ct = set(prev.get("top_decile") or []), set(cur.get("top_decile") or [])
    if pt and ct:
        kept = len(pt & ct) / len(pt)
        if kept < cfg.min_top_decile_overlap:
            issues.append(f"only {kept:.0%} of the previous top decile is still in it")
    return issues


class HealthCheck:
    """Run-level check for `score(run_check=...)`; keeps the summary it computed so the
    pipeline can store it with the run (the next run compares against it)."""

    def __init__(self, cfg: RunHealth, previous: tuple[dt.datetime, dict] | None = None,
                 ) -> None:
        self.cfg = cfg
        self.previous = previous
        self.summary: dict = {}

    def __call__(self, view: PitView, inc: pl.DataFrame, res: ScoreResult) -> list[str]:
        self.summary = summary(inc, res)
        if not self.cfg.enabled:
            return []
        issues = freshness(view, inc, self.cfg)
        if self.previous is not None:
            prev_as_of, prev = self.previous
            gap = (view.as_of - prev_as_of).days
            if 0 < gap <= self.cfg.drift_max_gap_days and prev:
                issues += drift(self.summary, prev, self.cfg)
        return issues
