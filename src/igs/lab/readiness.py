"""Evidence inventory for research promotion. This never enables live trading."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from igs.lab.artifacts import digest, verify, write_json
from igs.lab.strategies import RULES
from igs.provenance import validation_fingerprint
from igs.timeutil import utc_now


def review(
    output: Path,
    *,
    comparison: Path | None = None,
    dataset: Path | None = None,
    model: Path | None = None,
) -> dict:
    checks, evidence = [], {}

    def check(name, passed, detail):
        checks.append({"check": name, "status": "PASS" if passed else "BLOCKED", "detail": detail})

    fingerprint = validation_fingerprint()
    if comparison:
        report = json.loads(comparison.read_text())
        evidence["comparison_sha256"] = digest(comparison)
        check(
            "current_backtest_code",
            report.get("fingerprint") == fingerprint,
            "Regenerate comparison after any code/configuration change.",
        )
        variants = report.get("variants", {})
        for name in ("eligible_baseline", *RULES):
            v = variants.get(name, {})
            check(
                f"{name}_descriptive_coverage",
                v.get("status") == "EXPLORATORY"
                and v.get("invested_periods", 0) >= 12
                and v.get("incomplete_periods") == 0,
                "Needs 12 invested complete periods: a coverage floor, not proof of alpha.",
            )
    else:
        check("backtest_comparison", False, "No corrected real-data comparison supplied.")
    if dataset:
        data = verify(dataset)
        evidence["dataset_manifest_sha256"] = digest(dataset / "manifest.json")
        check(
            "feature_dataset",
            data.get("kind") == "igs-feature-dataset"
            and data.get("code_hash") == fingerprint
            and data.get("labelled_rows", 0) >= 100,
            "Requires current feature code and at least 100 labelled rows.",
        )
    else:
        check("feature_dataset", False, "No verified point-in-time feature bundle supplied.")
    if model:
        run = verify(model)
        evidence["model_manifest_sha256"] = digest(model / "manifest.json")
        check(
            "model_reproducibility",
            run.get("kind") == "igs-model-experiment"
            and run.get("code_hash") == fingerprint
            and run.get("paper_only") is True,
            "Current code hash and paper-only experiment required.",
        )
        check(
            "model_dataset_lineage",
            dataset is not None
            and run.get("dataset_manifest_hash") == evidence.get("dataset_manifest_sha256"),
            "Experiment must reference the supplied dataset hash.",
        )
    else:
        check("model_experiment", False, "No verified model experiment supplied.")
    # These cannot be inferred from software tests or gross model diagnostics.
    for name, detail in {
        "total_return_benchmark": "Verify actual TRI coverage and historical universe membership.",
        "net_cost_out_of_sample": "Independent costed OOS advantage, stress and failure analysis.",
        "prospective_paper_record": "Accumulate forward paper outcomes under a frozen protocol.",
        "broker_reconciliation": "Verify broker positions and orders; resolve every discrepancy.",
        "recovery_and_security": "Test restore, rollback, kill switch and access controls.",
        "operator_release": "Separate operator review is required for any new live strategy.",
    }.items():
        check(name, False, detail)
    result = {
        "generated_at": utc_now().isoformat(),
        "status": "BLOCKED",
        "scope": "new strategies and models only",
        "live_trading_enabled": False,
        "checks": checks,
        "evidence": evidence,
        "note": "An evidence inventory, not a production authorization or deployment check.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, result)
    return result


def operations(conn) -> dict:
    """Read stored execution state. No broker request or settings mutation."""
    now = utc_now()
    states = conn.execute("select status,count(*) from intraday_trade group by status").fetchall()
    unresolved = {"submitting", "uncertain", "exit_unprotected", "cancel_uncertain",
                  "square_off_failed"}
    blockers = sum(n for state, n in states if state in unresolved)
    stale = conn.execute(
        """select count(*) from intraday_trade where status in
        ('submitted','entry_open','entry_filled','squaring_off') and
        (last_checked_at is null or last_checked_at < %s)""",
        (now - dt.timedelta(minutes=15),),
    ).fetchone()[0]
    return {
        "generated_at": now.isoformat(),
        "stored_trade_states": dict(states),
        "unresolved_orders": blockers,
        "active_orders_not_checked_15m": stale,
        "status": "REVIEW" if blockers or stale else "NO_STORED_STATE_BLOCKER",
        "broker_positions_verified": False,
        "note": "Stored state only; closed-market orders can be stale. "
        "This does not reconcile the actual broker account.",
    }
