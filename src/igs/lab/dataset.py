"""PIT feature snapshots and separate exact-session future-return labels.

Labels deliberately use future prices; they never enter feature construction.
Missing entry/exit prices remain missing. These labels are gross stock returns,
not executable portfolio results or evidence of net alpha.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from pathlib import Path

import polars as pl

from igs.backtest.engine import _liquidity, benchmark_series, total_return_prices
from igs.config import load_backtest, load_red_flags, load_scoring, load_universe
from igs.dq import DQLog
from igs.factors import REGISTRY, base
from igs.lab import strategies
from igs.lab.artifacts import bundle, seal, write_json
from igs.pit.gate import require_gate
from igs.provenance import validation_fingerprint
from igs.score.run import evaluate_date, rank_history, trading_days
from igs.timeutil import end_of_day_ist, utc_now

FEATURES = (
    *strategies.FEATURES,
    "risk_adj_return_6m",
    "volatility_1y",
    "relative_volume_20d",
    "up_down_volume_20d",
)
KEYS = ["as_of", "company_id", "security_id"]
FEATURE_SCHEMA = {
    "as_of": pl.Datetime("us", "UTC"),
    "company_id": pl.Int64,
    "security_id": pl.Int64,
    **dict.fromkeys(FEATURES, pl.Float64),
}
LABEL_SCHEMA = {
    "as_of": pl.Datetime("us", "UTC"),
    "company_id": pl.Int64,
    "security_id": pl.Int64,
    "entry_date": pl.Date,
    "label_end": pl.Date,
    "label_available_at": pl.Datetime("us", "UTC"),
    "target": pl.Float64,
}


def exact_labels(
    features: pl.DataFrame, prices: pl.DataFrame, days: list[dt.date], horizon: int
) -> pl.DataFrame:
    if not 1 <= horizon <= 756:
        raise ValueError("horizon must be 1..756 exchange sessions")
    if days != sorted(set(days)):
        raise ValueError("calendar must contain unique, sorted sessions")
    price_map = {
        (r["company_id"], r["security_id"], r["trade_date"]): r["tr"] for r in prices.to_dicts()
    }
    if len(price_map) != prices.height:
        raise ValueError("duplicate company/security/session prices")
    labels = []
    for row in features.select(KEYS).to_dicts():
        signal = row["as_of"].astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
        i = bisect.bisect_right(days, signal)
        entry = days[i] if i < len(days) else None
        end = days[i + horizon] if i + horizon < len(days) else None
        start_px = price_map.get((row["company_id"], row["security_id"], entry))
        end_px = price_map.get((row["company_id"], row["security_id"], end))
        valid = all(v is not None and math.isfinite(v) and v > 0 for v in (start_px, end_px))
        labels.append(
            {
                **row,
                "entry_date": entry,
                "label_end": end,
                "label_available_at": end_of_day_ist(end) if end else None,
                "target": end_px / start_px - 1 if valid else None,
            }
        )
    return pl.DataFrame(labels, schema=LABEL_SCHEMA)


def signal_dates(days, start, end, frequency):
    selected = [d for d in days if start <= d <= end]
    if frequency == "daily":
        return selected
    if frequency not in ("weekly", "monthly"):
        raise ValueError("frequency must be daily, weekly or monthly")
    groups = {}
    for d in selected:
        key = d.isocalendar()[:2] if frequency == "weekly" else (d.year, d.month)
        groups[key] = d
    return sorted(groups.values())


def export(
    dataset,
    start: dt.date,
    end: dt.date,
    output: Path,
    *,
    horizon=21,
    frequency="monthly",
    provenance: dict | None = None,
) -> dict:
    require_gate()
    if start > end or end >= utc_now().date():
        raise ValueError("feature export needs start <= end < today")
    if not 1 <= horizon <= 756:
        raise ValueError("horizon must be 1..756 exchange sessions")
    sc, uc, rf = load_scoring(), load_universe(), load_red_flags()
    days = trading_days(dataset)
    dates = signal_dates(days, start, end, frequency)
    history = rank_history(dataset, sc, uc, set())
    rows, evidence, coverage, execution = [], [], [], []
    for date in dates:
        ev = evaluate_date(dataset, end_of_day_ist(date), sc, uc, rf, set(), history, days)
        # Rejected firms are retained in research snapshots: targets must not silently
        # erase bad outcomes. Strategies apply risk checks independently.
        ids = ev.universe.filter(pl.col("included"))["company_id"].to_list()
        readings = strategies.readings(ev.view)
        for name in FEATURES:
            if name not in readings:
                readings[name] = {r["company_id"]: r for r in REGISTRY[name].fn(ev.view).to_dicts()}
        primary = dict(
            base.primary_prices(ev.view).select("company_id", "security_id").unique().iter_rows()
        )
        liquidity = {r['company_id']: r for r in _liquidity(ev.view).to_dicts()}
        universe = {r['company_id']: r for r in ev.universe.to_dicts()}
        risk = {}
        for flag in ev.flags.to_dicts():
            if flag['severity'] == 'reject':
                risk.setdefault(flag['company_id'], []).append(flag)
        coverage.append({"date": str(date), "eligible": len(ids), "seen": ev.universe.height})
        for cid in ids:
            if cid not in primary:
                continue
            item = {"as_of": ev.as_of, "company_id": cid, "security_id": primary[cid]}
            detail = {name: readings[name].get(cid, {}) for name in FEATURES}
            for name, reading in detail.items():
                value = reading.get("value")
                item[name] = (
                    value
                    if (
                        reading.get("status") == "ok" and value is not None and math.isfinite(value)
                    )
                    else None
                )
            rows.append(item)
            evidence.append({**{key: item[key] for key in KEYS}, "readings": detail})
            hard = risk.get(cid, [])
            execution.append({**{key: item[key] for key in KEYS},
                'risk_clear': not strategies.governance_unknown(hard)
                and not any(f['status'] == 'tripped' for f in hard),
                'industry': universe[cid].get('industry'), 'bucket': universe[cid].get('bucket'),
                'adv': liquidity.get(cid, {}).get('adv'), 'vol': liquidity.get(cid, {}).get('vol')})
    features = pl.DataFrame(rows, schema=FEATURE_SCHEMA)
    prices = total_return_prices(dataset)
    labels = exact_labels(features, prices, days, horizon)
    dq = DQLog()
    benchmark, benchmark_name = benchmark_series(dataset, load_backtest().benchmark, dq)
    with bundle(output) as path:
        features.write_parquet(path / "features.parquet")
        labels.write_parquet(path / "labels.parquet")
        pl.DataFrame(execution, schema={**{k: FEATURE_SCHEMA[k] for k in KEYS},
            'risk_clear': pl.Boolean, 'industry': pl.Utf8, 'bucket': pl.Utf8,
            'adv': pl.Float64, 'vol': pl.Float64}).write_parquet(path/'execution.parquet')
        prices.filter(pl.col('trade_date') >= start).write_parquet(path/'prices.parquet')
        benchmark.filter(pl.col('trade_date') >= start).write_parquet(path/'benchmark.parquet')
        write_json(path/'calendar.json', [str(d) for d in days if d >= start])
        with (path / "evidence.jsonl").open("w", encoding="utf-8") as stream:
            import json

            for item in evidence:
                stream.write(json.dumps(item, default=str, allow_nan=False) + "\n")
        write_json(path / "coverage.json", coverage)
        return seal(
            path,
            {
                "kind": "igs-feature-dataset",
                "schema_version": 1,
                "created_at": utc_now().isoformat(),
                "features": list(FEATURES),
                "horizon_sessions": horizon,
                "frequency": frequency,
                'benchmark_name': benchmark_name,
                'benchmark_warnings': [issue.message for issue in dq.issues],
                "start": str(start),
                "end": str(end),
                "rows": features.height,
                "labelled_rows": labels["target"].count(),
                "code_hash": validation_fingerprint(),
                "provenance": provenance or {},
                "strategy_specification": strategies.specification(),
                "scoring_config": sc.model_dump(),
                "universe_config": uc.model_dump(),
                "red_flags_config": rf.model_dump(),
                "limitations": [
                    "Gross exact-close labels are not costed strategy P&L.",
                    "Missing/delisted exits remain unknown, not last-price liquidations.",
                    "Recorded publication times may omit ingestion-history limitations.",
                    "Fundamental features are for long-term/swing research, not intraday models.",
                ],
            },
        )
