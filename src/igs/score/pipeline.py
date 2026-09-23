"""Score from the database and persist the run."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from igs.config import (
    RedFlagsConfig,
    ScoringConfig,
    UniverseConfig,
    load_red_flags,
    load_scoring,
    load_universe,
)
from igs.pit.loader import load_dataset
from igs.score.persist import persist_run
from igs.score.run import ScoreRun, explanations, score

HISTORY_YEARS = 7


def score_from_db(conn, as_of: dt.datetime, ic_status_path: Path | None,
                  sc: ScoringConfig | None = None, uc: UniverseConfig | None = None,
                  rf: RedFlagsConfig | None = None, check_gate: bool = True
                  ) -> tuple[int, ScoreRun]:
    sc, uc, rf = sc or load_scoring(), uc or load_universe(), rf or load_red_flags()
    as_of_date = as_of.date()
    dataset = load_dataset(conn, dt.date(as_of_date.year - HISTORY_YEARS, 1, 1), as_of_date)
    run = score(dataset, as_of, sc, uc, rf, ic_status_path, check_gate=check_gate)
    with conn.cursor() as cur:
        cur.execute("select company_id, name from company")
        names = dict(cur.fetchall())
    texts = explanations(run, dataset.tables["filings"], names)
    config = {"scoring": sc.model_dump(), "universe": uc.model_dump(),
              "red_flags": rf.model_dump()}
    run_id = persist_run(conn, run, texts, config)
    run.dq.persist(conn)
    conn.commit()
    return run_id, run
