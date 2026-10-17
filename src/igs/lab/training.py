"""Chronological, purged research training. Models are never promoted or traded here."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import math
from pathlib import Path

import polars as pl

from igs.lab.artifacts import bundle, digest, seal, verify, write_json
from igs.lab.dataset import KEYS
from igs.provenance import ROOT, validation_fingerprint
from igs.timeutil import end_of_day_ist, utc_now


def utc_columns(frame: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    for name in columns:
        dtype = frame.schema.get(name)
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
            raise ValueError(f'{name} must be a timezone-aware timestamp')
    return frame.with_columns(pl.col(name).dt.convert_time_zone('UTC') for name in columns)


def purged_split(
    frame: pl.DataFrame, validation_start: dt.date, holdout_start: dt.date, gap_days: int = 5
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Expanding-time split; training labels must be known before the next split.

    The extra gap is calendar days. No observations after validation are used for
    training, so a symmetric post-validation embargo is unnecessary here.
    """
    if validation_start >= holdout_start or gap_days < 0:
        raise ValueError("Need validation_start < holdout_start and gap_days >= 0")
    frame = utc_columns(frame, ['as_of', 'label_available_at'])
    valid = end_of_day_ist(validation_start - dt.timedelta(days=1)).astimezone(dt.UTC)
    holdout = end_of_day_ist(holdout_start - dt.timedelta(days=1)).astimezone(dt.UTC)
    known = frame.filter(
        pl.col("target").is_not_null()
        & pl.col("target").is_finite()
        & pl.col("label_available_at").is_not_null()
    )
    if known.filter(pl.col("label_available_at") <= pl.col("as_of")).height:
        raise ValueError("Future labels must become available after their signal")
    train = known.filter(
        (pl.col("as_of") <= valid)
        & (pl.col("label_available_at") < valid - dt.timedelta(days=gap_days))
    )
    validation = known.filter(
        (pl.col("as_of") > valid)
        & (pl.col("as_of") <= holdout)
        & (pl.col("label_available_at") < holdout - dt.timedelta(days=gap_days))
    )
    test = known.filter(pl.col("as_of") > holdout)
    return train, validation, test


def fit_transform(frame: pl.DataFrame, features: list[str]) -> dict:
    """Train-only medians/scales; missing indicators retained for both algorithms."""
    medians, means, scales, missing = [], [], [], []
    for name in features:
        series = frame[name].cast(pl.Float64).fill_nan(None)
        median = series.median()
        if median is None:
            raise ValueError(f"Training feature has no observations: {name}")
        filled = series.fill_null(median)
        medians.append(median)
        means.append(filled.mean())
        scales.append(filled.std(ddof=0) or 1.0)
        missing.append(series.null_count() / len(series))
    return {
        "features": features,
        "medians": medians,
        "means": means,
        "scales": scales,
        "missing_rates": missing,
    }


def transform(frame: pl.DataFrame, preprocessing: dict):
    import numpy as np

    features = preprocessing["features"]
    x = frame.select(features).to_numpy().astype(float)
    absent = ~np.isfinite(x)
    filled = np.where(absent, np.asarray(preprocessing["medians"]), x)
    return np.concatenate(
        (
            (filled - np.asarray(preprocessing["means"])) / np.asarray(preprocessing["scales"]),
            absent.astype(float) - np.asarray(preprocessing["missing_rates"]),
        ),
        axis=1,
    )


def diagnostics(predictions: pl.DataFrame) -> dict:
    per_date = predictions.group_by("as_of").agg(
        pl.len().alias("n"), pl.corr("prediction", "target", method="spearman").alias("ic")
    )
    usable = per_date.filter((pl.col("n") >= 5) & pl.col("ic").is_finite())
    errors = predictions["prediction"] - predictions["target"]
    buckets = (
        predictions.with_columns(
            ((pl.col("prediction").rank("ordinal") - 1) * 5 / pl.len())
            .floor()
            .cast(pl.Int64)
            .clip(0, 4)
            .alias("bucket")
        )
        .group_by("bucket")
        .agg(pl.len().alias("n"), pl.col("prediction").mean(), pl.col("target").mean())
        .sort("bucket")
    )
    return {
        "rows": predictions.height,
        "dates": per_date.height,
        "ic_dates": usable.height,
        "mean_date_ic": usable["ic"].mean(),
        "mae": errors.abs().mean(),
        "rmse": math.sqrt((errors**2).mean()),
        "calibration": buckets.to_dicts(),
        "interpretation": "Gross return diagnostics; no costed portfolio or significance claim.",
    }


def drift(frame: pl.DataFrame, preprocessing: dict) -> dict:
    metrics, alerts = {}, []
    if frame.is_empty():
        return {"status": "INCONCLUSIVE", "features": {}, "alerts": ["No observations"]}
    for i, name in enumerate(preprocessing["features"]):
        values = frame[name].cast(pl.Float64)
        values = values.filter(values.is_not_null() & values.is_finite())
        missing_rate = 1 - len(values) / frame.height
        shift = (
            (values.mean() - preprocessing["means"][i]) / preprocessing["scales"][i]
            if len(values)
            else None
        )
        missing_change = missing_rate - preprocessing["missing_rates"][i]
        metrics[name] = {
            "mean_shift_train_std": shift,
            "missing_rate": missing_rate,
            "missing_rate_change": missing_change,
        }
        if shift is None or abs(shift) > 3 or missing_change > 0.2:
            alerts.append(name)
    return {
        "status": "REVIEW" if alerts else "NO_THRESHOLD_BREACH",
        "features": metrics,
        "alerts": alerts,
        "note": "Heuristic monitoring thresholds, not calibrated probabilities.",
    }


def train(
    source: Path,
    output: Path,
    validation_start: dt.date,
    holdout_start: dt.date,
    *,
    algorithm="ridge",
    gap_days=5,
    features: list[str] | None = None,
) -> dict:
    from igs.pit.gate import require_gate

    require_gate()
    manifest = verify(source)
    if manifest.get("kind") != "igs-feature-dataset":
        raise ValueError("Expected an IGS feature dataset")
    if algorithm not in ("ridge", "lightgbm"):
        raise ValueError("Supported algorithms: ridge, lightgbm")
    names = features or manifest["features"]
    if not names or len(names) != len(set(names)) or not set(names) <= set(manifest["features"]):
        raise ValueError("Select unique feature names declared by the dataset")
    frame = utc_columns(pl.read_parquet(source / "features.parquet"), ['as_of'])
    labels = utc_columns(pl.read_parquet(source / "labels.parquet"),
                         ['as_of', 'label_available_at'])
    if frame.select(KEYS).is_duplicated().any() or labels.select(KEYS).is_duplicated().any():
        raise ValueError("Duplicate feature/label keys")
    if frame.select(KEYS).null_count().row(0) != (0, 0, 0):
        raise ValueError("Missing feature identity")
    if frame.height != labels.height or frame.join(labels, on=KEYS, how="anti").height:
        raise ValueError("Feature and label identities differ")
    frame = frame.join(labels, on=KEYS, validate="1:1").sort(KEYS)
    if frame.filter(pl.col('as_of') >= utc_now()).height:
        raise ValueError('Future feature timestamps are invalid')
    labelled = frame.filter(pl.col('target').is_not_null())
    if labelled.filter(
        pl.col('entry_date').is_null() | pl.col('label_end').is_null()
        | (pl.col('label_end') <= pl.col('entry_date'))
        | (pl.col('entry_date') <= pl.col('as_of').dt.convert_time_zone('Asia/Kolkata').dt.date())
        | (pl.col('label_available_at').dt.convert_time_zone('Asia/Kolkata').dt.date()
           < pl.col('label_end'))
    ).height:
        raise ValueError('Invalid future-return label chronology')
    if any(frame[name].drop_nulls().is_infinite().any() for name in names):
        raise ValueError("Infinite feature values are invalid")
    if frame.filter(pl.col("label_available_at") > utc_now()).height:
        raise ValueError("Dataset contains unmatured labels")
    train_frame, validation, holdout = purged_split(
        frame, validation_start, holdout_start, gap_days
    )
    if train_frame.height < 100 or train_frame["as_of"].n_unique() < 12:
        raise ValueError("Need at least 100 labelled training rows across 12 signal dates")
    if any(part.height < 20 or part["as_of"].n_unique() < 2 for part in (validation, holdout)):
        raise ValueError("Need at least 20 labelled rows across 2 dates in validation and holdout")
    preprocessing = fit_transform(train_frame, names)
    x, y = transform(train_frame, preprocessing), train_frame["target"].to_numpy()
    import numpy as np

    parameters = {"algorithm": algorithm, "seed": 1729}
    model = None
    if algorithm == "ridge":
        parameters["alpha"] = 10.0
        intercept = float(y.mean())
        coefficients = np.linalg.solve(x.T @ x + 10 * np.eye(x.shape[1]), x.T @ (y - intercept))
        model = {"coefficients": coefficients.tolist(), "intercept": intercept}

        def predict(part):
            return transform(part, preprocessing) @ coefficients + intercept
    else:
        try:
            import lightgbm as lgb
        except ImportError:
            raise ValueError("LightGBM needs `uv sync --group ml`") from None
        parameters.update(
            {
                "objective": "regression",
                "num_leaves": 15,
                "learning_rate": 0.03,
                "min_data_in_leaf": 30,
                "verbosity": -1,
                "num_threads": 1,
                "deterministic": True,
                "force_col_wise": True,
            }
        )
        booster = lgb.train(
            {k: v for k, v in parameters.items() if k != "algorithm"},
            lgb.Dataset(x, label=y),
            num_boost_round=100,
        )

        def predict(part):
            return booster.predict(transform(part, preprocessing))

    reports, predictions = {}, []
    for name, part in [("validation", validation), ("holdout", holdout)]:
        predicted = part.select(*KEYS, "label_end", "target").with_columns(
            pl.Series("prediction", predict(part)), pl.lit(name).alias("split")
        )
        reports[name] = diagnostics(predicted)
        predictions.append(predicted)
    with bundle(output) as path:
        write_json(path / "preprocessing.json", preprocessing)
        if model is not None:
            write_json(path / "model.json", model)
        else:
            booster.save_model(str(path / "model.txt"))
        pl.concat(predictions).write_parquet(path / "predictions.parquet")
        write_json(path / "metrics.json", reports)
        write_json(path / "drift.json", drift(holdout, preprocessing))
        return seal(
            path,
            {
                "kind": "igs-model-experiment",
                "schema_version": 1,
                "created_at": utc_now().isoformat(),
                "stage": "research",
                "paper_only": True,
                "parameters": parameters,
                "dataset_manifest_hash": digest(source / "manifest.json"),
                "dataset_code_hash": manifest["code_hash"],
                "code_hash": validation_fingerprint(),
                "lock_hash": digest(ROOT / "uv.lock"),
                "features": names,
                "horizon_sessions": manifest["horizon_sessions"],
                "feature_frequency": manifest["frequency"],
                "versions": {
                    name: importlib.metadata.version(name)
                    for name in ["numpy", "polars"]
                    + (["lightgbm"] if algorithm == "lightgbm" else [])
                },
                "split": {
                    "validation_start": str(validation_start),
                    "holdout_start": str(holdout_start),
                    "gap_calendar_days": gap_days,
                    "train_rows": train_frame.height,
                    "train_latest_label": str(train_frame["label_available_at"].max()),
                    "validation_rows": validation.height,
                    "holdout_rows": holdout.height,
                    "excluded_rows": frame.height
                    - train_frame.height
                    - validation.height
                    - holdout.height,
                },
                "limitations": [
                    "Historical holdout is exploratory unless independently preregistered.",
                    "Repeated runs against this holdout do not create independent validation.",
                    "No automatic promotion; gross IC alone is insufficient.",
                ],
            },
        )


def predict_snapshot(model_path: Path, source: Path, output: Path) -> dict:
    from igs.pit.gate import require_gate
    require_gate()
    manifest, dataset = verify(model_path), verify(source)
    if manifest.get("kind") != "igs-model-experiment" or not manifest.get("paper_only"):
        raise ValueError("Only paper research models are supported")
    if dataset.get("kind") != "igs-feature-dataset":
        raise ValueError("Expected an IGS feature dataset")
    if (
        dataset["code_hash"] != manifest["dataset_code_hash"]
        or dataset["horizon_sessions"] != manifest["horizon_sessions"]
        or dataset["frequency"] != manifest["feature_frequency"]
    ):
        raise ValueError("Feature code, horizon or frequency changed; retrain explicitly")
    preprocessing = json.loads((model_path / "preprocessing.json").read_text())
    frame = utc_columns(pl.read_parquet(source / "features.parquet"), ['as_of'])
    if frame.is_empty():
        raise ValueError("No feature rows to predict")
    if frame.select(KEYS).is_duplicated().any() or any(frame.select(KEYS).null_count().row(0)):
        raise ValueError('Invalid or duplicate prediction identity')
    if frame.filter(pl.col('as_of') >= utc_now()).height:
        raise ValueError('Future feature timestamps are invalid')
    cutoff = dt.datetime.fromisoformat(manifest["split"]["train_latest_label"])
    if frame["as_of"].min() <= cutoff:
        raise ValueError("Predictions must be after the latest training label")
    x = transform(frame, preprocessing)
    if manifest["parameters"]["algorithm"] == "ridge":
        import numpy as np

        model = json.loads((model_path / "model.json").read_text())
        predicted = x @ np.asarray(model["coefficients"]) + model["intercept"]
    else:
        import lightgbm as lgb

        predicted = lgb.Booster(model_file=str(model_path / "model.txt")).predict(x)
    with bundle(output) as path:
        result = frame.select(KEYS).with_columns(pl.Series("prediction", predicted))
        result.write_parquet(path / "predictions.parquet")
        write_json(path / "drift.json", drift(frame, preprocessing))
        return seal(
            path,
            {
                "kind": "igs-paper-predictions",
                "created_at": utc_now().isoformat(),
                "paper_only": True,
                "rows": frame.height,
                "model_manifest_hash": digest(model_path / "manifest.json"),
                "dataset_manifest_hash": digest(source / "manifest.json"),
            },
        )
