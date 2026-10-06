"""Fundamentals validation report (step 2 sign-off)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from igs.recon.checks import CheckResult
from igs.recon.report import write
from igs.timeutil import utc_now
from igs.xbrl.checks import (
    compare_expected,
    identity_breaks,
    latest,
    load_hand_checked,
    missing_quarters,
)
from igs.xbrl.load import facts_frame


def _company_ids(conn, symbols: list[str]) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("""select distinct on (si.id_value) si.id_value, s.company_id
                       from security_identifier si join security s using (security_id)
                       where si.id_type = 'NSE_SYMBOL' and si.id_value = any(%s)
                       order by si.id_value, si.valid_to is null desc, si.valid_from desc""",
                    (symbols,))
        return dict(cur.fetchall())


def validate_fundamentals(conn, out_dir: Path) -> tuple[list[CheckResult], Path]:
    cfg = load_hand_checked()
    symbols = [c["symbol"] for c in cfg["companies"]]
    ids = _company_ids(conn, symbols)
    facts = facts_frame(conn, list(ids.values()) or [-1])
    results: list[CheckResult] = []

    unmapped = [s for s in symbols if s not in ids]
    results.append(CheckResult(
        "hand_checked_mapped", "fail" if unmapped else "pass",
        f"{len(ids)}/{len(symbols)} companies in the instrument master",
        pl.DataFrame({"symbol": unmapped}) if unmapped else None))

    q = facts.filter((pl.col("period_type") == "Q")
                     & pl.col("concept").is_in(["revenue", "interest_earned"]))
    cover = (q.group_by("company_id").agg(pl.col("period_end").n_unique().alias("quarters"))
              .join(pl.DataFrame({"symbol": list(ids), "company_id": list(ids.values())}),
                    on="company_id", how="right").fill_null(0))
    thin = cover.filter(pl.col("quarters") < 8)
    results.append(CheckResult(
        "eight_quarters", "fail" if thin.height else "pass",
        f"{cover.height - thin.height}/{cover.height} companies have >= 8 quarters of revenue",
        thin if thin.height else None))

    gaps = missing_quarters(facts)
    results.append(CheckResult("missing_quarters", "warn" if gaps.height else "pass",
                               f"{gaps.height} missing quarter(s) (reported, not filled)",
                               gaps if gaps.height else None))

    breaks = identity_breaks(facts)
    results.append(CheckResult("accounting_identities", "fail" if breaks.height else "pass",
                               f"{breaks.height} identity breaks within filings",
                               breaks if breaks.height else None))

    expected = cfg.get("expected") or []
    if expected:
        cmp = compare_expected(latest(facts), expected, ids)
        bad = cmp.filter(pl.col("status") != "ok")
        results.append(CheckResult("hand_checked_values", "fail" if bad.height else "pass",
                                   f"{cmp.height - bad.height}/{cmp.height} hand-entered values "
                                   "match", cmp))
    else:
        results.append(CheckResult("hand_checked_values", "warn",
                                   "no hand-entered values yet in config/hand_checked.yaml"))
    path = write(results, None, out_dir / f"fundamentals_validation_{utc_now().date()}.md",
                 "Fundamentals validation (20 hand-checked companies)", utc_now())
    return results, path
