"""Write a ScoreRun to the database (append-only history of rankings)."""

from __future__ import annotations

import json
import math

import polars as pl
import psycopg

from igs.score.run import ScoreRun


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def persist_run(conn: psycopg.Connection, run: ScoreRun, explanations: dict[int, str],
                config: dict) -> int:
    dq = {"error": run.dq.count("error"), "warn": run.dq.count("warn"),
          "issues": [i.message for i in run.dq.issues][:50]}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""insert into score_run (as_of, gate_fingerprint, ic_status_generated_at,
                           dropped_factors, config, dq_summary)
                       values (%s, %s, %s, %s, %s, %s) returning run_id""",
                    (run.as_of, run.gate_fingerprint, run.ic_status_generated_at,
                     json.dumps(run.dropped_factors), json.dumps(config, default=str),
                     json.dumps(dq)))
        run_id = cur.fetchone()[0]
        with cur.copy("""copy score_result (run_id, company_id, symbol, mcap_cr, bucket, industry,
                             sector, composite, coverage, rank, scored, tier, tier_reason,
                             explanation) from stdin""") as cp:
            for r in run.results.iter_rows(named=True):
                cp.write_row((run_id, r["company_id"], r.get("symbol"), _clean(r["mcap_cr"]),
                              r["bucket"], r["industry"], r["sector"], _clean(r["composite"]),
                              _clean(r["coverage"]), r["rank"], r["scored"], r["tier"],
                              r["tier_reason"], explanations.get(r["company_id"], "")))
        with cur.copy("copy score_pillar (run_id, company_id, pillar, score, coverage) "
                      "from stdin") as cp:
            for r in run.pillars.iter_rows(named=True):
                cp.write_row((run_id, r["company_id"], r["pillar"], _clean(r["score"]),
                              _clean(r["coverage"])))
        cols = ["company_id", "factor", "pillar", "status", "value", "winsorized", "z",
                "peer_percentile", "peer_level", "peer_group", "peer_count", "contribution",
                "detail", "source_fact_ids", "source_filing_ids"]
        f = run.factors.select([c for c in cols if c in run.factors.columns])
        with cur.copy(f"copy score_factor (run_id, {', '.join(f.columns)}) from stdin") as cp:
            for r in f.iter_rows():
                cp.write_row((run_id, *[_clean(v) for v in r]))
        with cur.copy("""copy red_flag_result (run_id, company_id, flag, status, message,
                             evidence, source_ids, source_urls) from stdin""") as cp:
            for r in run.flags.iter_rows(named=True):
                cp.write_row((run_id, r["company_id"], r["flag"], r["status"], r["message"],
                              r["evidence"], r["source_ids"], r["source_urls"]))
    return run_id


def tier_counts(results: pl.DataFrame) -> dict[str, int]:
    return dict(results.group_by("tier").len().iter_rows())
