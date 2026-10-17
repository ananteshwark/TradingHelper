"""Research commands, with database-free model operations and explicit output paths."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from igs.lab.artifacts import write_json


def run(args) -> int:
    try:
        return _run(args)
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print(f"Research stopped: {exc}")
        return 2


def _run(args) -> int:
    if args.action == 'portfolio':
        from igs.lab.portfolio import backtest
        result = backtest(Path(args.dataset), Path(args.predictions), Path(args.output),
                          size=args.size, industry_cap=args.industry_cap)
    elif args.action == "train":
        from igs.lab.training import train

        result = train(
            Path(args.dataset),
            Path(args.output),
            dt.date.fromisoformat(args.validation),
            dt.date.fromisoformat(args.holdout),
            algorithm=args.algorithm,
            gap_days=args.gap_days,
            features=args.features.split(",") if args.features else None,
        )
    elif args.action == "predict":
        from igs.lab.training import predict_snapshot

        result = predict_snapshot(Path(args.model), Path(args.dataset), Path(args.output))
    elif args.action == "readiness":
        from igs.lab.readiness import review

        result = review(
            Path(args.output),
            comparison=Path(args.comparison) if args.comparison else None,
            dataset=Path(args.dataset) if args.dataset else None,
            model=Path(args.model) if args.model else None,
        )
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "BLOCKED" else 0
    elif args.action == "track":
        from igs.lab.tracking import publish

        print("MLflow run:", publish(Path(args.model), args.url, args.experiment_id))
        return 0
    else:
        from igs.config import load_red_flags, load_scoring, load_universe
        from igs.db import connect
        from igs.lab import dataset, readiness, strategies
        from igs.pit.gate import require_gate
        from igs.pit.loader import load_dataset
        from igs.provenance import run_provenance
        from igs.score.run import evaluate_date, rank_history, trading_days
        from igs.timeutil import end_of_day_ist, utc_now
        from igs.universe import price_series

        with connect() as conn:
            conn.execute("set transaction isolation level repeatable read, read only")
            if args.action == "operations":
                result = readiness.operations(conn)
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                write_json(Path(args.output), result)
            else:
                require_gate()
                start = dt.date.fromisoformat(
                    args.start if args.action == "features" else args.as_of
                )
                if start >= utc_now().date():
                    raise ValueError("Use a completed date before today")
                sc, uc, rf = load_scoring(), load_universe(), load_red_flags()
                data = load_dataset(
                    conn,
                    dt.date(start.year - 7, 1, 1),
                    utc_now().date(),
                    series=tuple(price_series(uc)),
                )
                if args.action == "features":
                    result = dataset.export(
                        data,
                        start,
                        dt.date.fromisoformat(args.end),
                        Path(args.output),
                        horizon=args.horizon,
                        frequency=args.frequency,
                        provenance=run_provenance(conn),
                    )
                else:
                    history = rank_history(data, sc, uc, set())
                    ev = evaluate_date(
                        data, end_of_day_ist(start), sc, uc, rf, set(), history, trading_days(data)
                    )
                    result = {
                        "specification": strategies.specification(),
                        "provenance": run_provenance(conn),
                        "as_of": str(start),
                        "companies": strategies.assess(
                            ev.view, ev.results["company_id"].to_list(), ev.flags
                        ),
                    }
                    from igs.lab.artifacts import bundle, seal

                    with bundle(Path(args.output)) as path:
                        write_json(path / "strategies.json", result)
                        seal(
                            path,
                            {
                                "kind": "igs-strategy-snapshot",
                                "as_of": str(start),
                                "specification": result["specification"],
                            },
                        )
    print(f"Research {args.action}: {args.output}")
    return 0


def register(actions) -> None:
    portfolio = actions.add_parser('portfolio', help='costed monthly model paper portfolio')
    for name in ('dataset', 'predictions', 'output'):
        portfolio.add_argument('--'+name, required=True)
    portfolio.add_argument('--size', type=int, default=20)
    portfolio.add_argument('--industry-cap', type=float, default=.25)
    profiles = actions.add_parser("strategies", help="point-in-time experimental strategy evidence")
    profiles.add_argument("--as-of", required=True)
    profiles.add_argument("--output", required=True)
    features = actions.add_parser("features", help="export versioned features and separate labels")
    for name in ("start", "end", "output"):
        features.add_argument("--" + name, required=True)
    features.add_argument("--horizon", type=int, default=21, help="exchange sessions; 1..756")
    features.add_argument("--frequency", choices=("daily", "weekly", "monthly"), default="monthly")
    train = actions.add_parser("train", help="fit a purged chronological paper-only experiment")
    for name in ("dataset", "validation", "holdout", "output"):
        train.add_argument("--" + name, required=True)
    train.add_argument("--algorithm", choices=("ridge", "lightgbm"), default="ridge")
    train.add_argument("--gap-days", type=int, default=5)
    train.add_argument("--features", help="comma-separated subset frozen before evaluation")
    predict = actions.add_parser("predict", help="paper predictions and feature drift report")
    for name in ("model", "dataset", "output"):
        predict.add_argument("--" + name, required=True)
    ready = actions.add_parser(
        "readiness", help="report remaining promotion evidence; never enables live"
    )
    for name in ("comparison", "dataset", "model"):
        ready.add_argument("--" + name)
    ready.add_argument("--output", required=True)
    operations = actions.add_parser(
        "operations", help="audit stored order states without broker calls"
    )
    operations.add_argument("--output", required=True)
    track = actions.add_parser("track", help="explicitly publish metrics to your MLflow server")
    for name in ("model", "url", "experiment-id"):
        track.add_argument("--" + name, required=True)
    for command in (profiles, features, train, predict, ready, operations, track, portfolio):
        command.set_defaults(fn=run)
