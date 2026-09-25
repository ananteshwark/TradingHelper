"""Score from the database and persist the run."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
from psycopg.pq import TransactionStatus

from igs.config import (
    RedFlagsConfig,
    ScoringConfig,
    UniverseConfig,
    load_red_flags,
    load_scoring,
    load_universe,
)
from igs.pit.loader import load_dataset
from igs.provenance import run_provenance
from igs.score.health import HealthCheck
from igs.score.persist import persist_run
from igs.score.run import ScoreRun, explanations, score
from igs.universe import price_series

HISTORY_YEARS = 7


def previous_health(conn, as_of: dt.datetime) -> tuple[dt.datetime, dict] | None:
    """The most recent earlier run's health summary, for drift checks."""
    with conn.cursor() as cur:
        cur.execute("""select as_of, health from score_run where as_of < %s
                       order by as_of desc, run_id desc limit 1""", (as_of,))
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], (row[1] or {}).get("summary") or {}


def universe_summary(universe: pl.DataFrame, min_quarters: int) -> dict:
    """How many companies were seen, how many made the universe, and why the rest did not
    (the per-company quarter counts folded into one reason)."""
    if universe.is_empty():
        return {"seen": 0, "included": 0, "excluded": {}, "shares_from_capital": 0}
    reasons = (universe.filter(~pl.col("included"))
               .select(pl.when(pl.col("reason").str.starts_with("only "))
                         .then(pl.lit(f"fewer than {min_quarters} quarters of results loaded"))
                         .otherwise(pl.col("reason")).alias("reason"))
               .group_by("reason").len().sort(["len", "reason"], descending=[True, False]))
    from_capital = (int((universe["shares_source"] == "paid_up_capital").sum())
                    if "shares_source" in universe.columns else 0)
    return {"seen": universe.height, "included": int(universe["included"].sum()),
            "excluded": dict(reasons.iter_rows()), "shares_from_capital": from_capital}


def score_from_db(conn, as_of: dt.datetime, ic_status_path: Path | None,
                  sc: ScoringConfig | None = None, uc: UniverseConfig | None = None,
                  rf: RedFlagsConfig | None = None, check_gate: bool = True
                  ) -> tuple[int, ScoreRun]:
    sc, uc, rf = sc or load_scoring(), uc or load_universe(), rf or load_red_flags()
    if conn.info.transaction_status == TransactionStatus.IDLE and not conn.autocommit:
        # New CLI/daily scoring transactions see a coherent view while ingestion runs.
        conn.execute("set transaction isolation level repeatable read")
    as_of_date = as_of.date()
    dataset = load_dataset(conn, dt.date(as_of_date.year - HISTORY_YEARS, 1, 1), as_of_date,
                           series=tuple(price_series(uc)))
    health = HealthCheck(sc.run_health, previous_health(conn, as_of))
    run = score(dataset, as_of, sc, uc, rf, ic_status_path, check_gate=check_gate,
                run_check=health)
    with conn.cursor() as cur:
        cur.execute("select company_id, name from company")
        names = dict(cur.fetchall())
    texts = explanations(run, dataset.tables["filings"], names)
    config = {"provenance": run_provenance(conn), "scoring": sc.model_dump(),
              "universe": uc.model_dump(),
              "red_flags": rf.model_dump()}
    run_id = persist_run(conn, run, texts, config,
                         {"issues": run.run_issues, "summary": health.summary,
                          "universe": universe_summary(run.universe, uc.min_filing_quarters)})
    run.dq.persist(conn)
    conn.commit()
    return run_id, run
