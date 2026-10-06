"""Plausibility bounds on raw factor values.

A value outside its configured range (scoring.yaml `plausibility`) is far more
likely a data error than a fact. It is not clipped or replaced: the factor's
status becomes "implausible", its value is left out of the score (the company's
coverage falls accordingly), the raw value is kept in the detail, the event is
logged as a data-quality issue, and the stock cannot be High conviction until the
filing behind it has been checked.
"""

from __future__ import annotations

import polars as pl

from igs.dq import DQLog

IMPLAUSIBLE = "implausible"
SCHEMA = {"company_id": pl.Int64, "factor": pl.Utf8, "value": pl.Float64, "lo": pl.Float64,
          "hi": pl.Float64}


def apply(outputs: dict[str, pl.DataFrame], bounds: dict[str, tuple[float, float]],
          dq: DQLog | None = None) -> tuple[dict[str, pl.DataFrame], pl.DataFrame]:
    out, found = {}, []
    for name, df in outputs.items():
        if name not in bounds or df.height == 0:
            out[name] = df
            continue
        lo, hi = bounds[name]
        bad = (pl.col("status") == "ok") & ((pl.col("value") < lo) | (pl.col("value") > hi))
        hits = df.filter(bad)
        if hits.height:
            found.append(hits.select("company_id", pl.lit(name).alias("factor"), "value",
                                     pl.lit(float(lo)).alias("lo"), pl.lit(float(hi)).alias("hi")))
            if dq is not None:
                dq.emit("warn", "implausible_factor_value",
                        f"{hits.height} value(s) of {name} outside [{lo:g}, {hi:g}] left out of "
                        "the score", details={"company_ids": hits["company_id"].to_list()[:50],
                                              "values": hits["value"].to_list()[:50]})
        out[name] = df.with_columns(
            pl.when(bad).then(pl.lit(IMPLAUSIBLE)).otherwise(pl.col("status")).alias("status"),
            pl.when(bad).then(pl.concat_str([
                pl.lit('{"implausible_value": '), pl.col("value").cast(pl.Utf8),
                pl.lit(f', "bounds": [{float(lo)}, {float(hi)}], "inputs": '),
                pl.col("detail"), pl.lit("}")]))
              .otherwise(pl.col("detail")).alias("detail"),
            pl.when(bad).then(None).otherwise(pl.col("value")).alias("value"))
    implausible = pl.concat(found) if found else pl.DataFrame(schema=SCHEMA)
    return out, implausible.cast(SCHEMA)


def blockers(implausible: pl.DataFrame) -> pl.DataFrame:
    return implausible.select(
        "company_id", pl.format("implausible {} value {} (outside {} to {}; possible data error)",
                                "factor", pl.col("value").round(3), "lo", "hi").alias("reason"))
