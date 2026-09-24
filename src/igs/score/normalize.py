"""Cross-sectional normalisation and the composite score.

For each factor on one date:
  1. winsorise ok values at the configured percentiles across the universe;
  2. flip the sign of lower-is-better factors;
  3. z-score WITHIN the industry peer group; an industry with fewer than
     min_peers valid values falls back to its sector; if the sector is also
     too thin the z-score is left empty (never computed against the whole
     market, never imputed);
  4. record the peer percentile and which peer level was used.
Pillar score = weighted mean of available factor z-scores, weights
renormalised over the factors that apply to the company; composite =
weighted sum of available pillar scores, renormalised likewise. Coverage is
reported, and too little coverage leaves the composite empty.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from igs.config import ScoringConfig
from igs.factors.base import RESULT_SCHEMA
from igs.factors.registry import REGISTRY

MIN_PILLAR_COVERAGE = 0.5      # share of a pillar's applicable weight with a z-score
MIN_COMPOSITE_COVERAGE = 0.6   # share of total pillar weight with a pillar score


@dataclass(frozen=True)
class ScoreResult:
    factors: pl.DataFrame     # one row per company x factor
    pillars: pl.DataFrame     # one row per company x pillar
    composite: pl.DataFrame   # one row per company


def factor_long(outputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Stack factor outputs into long form with pillar and direction."""
    frames = []
    for name, df in outputs.items():
        spec = REGISTRY[name]
        frames.append(df.with_columns(pl.lit(name).alias("factor"),
                                      pl.lit(spec.pillar).alias("pillar"),
                                      pl.lit(spec.higher_is_better).alias("higher_is_better")))
    if not frames:   # every factor dropped (e.g. by walk-forward IC selection)
        return pl.DataFrame(schema={**RESULT_SCHEMA, "factor": pl.Utf8, "pillar": pl.Utf8,
                                    "higher_is_better": pl.Boolean})
    return pl.concat(frames, how="vertical_relaxed")


def normalise(long: pl.DataFrame, peers: pl.DataFrame, cfg: ScoringConfig) -> pl.DataFrame:
    """long: company_id, factor, pillar, higher_is_better, value, status, ...
    peers: company_id, industry, sector (the universe on this date)."""
    lo, hi = cfg.winsorize.lower_pct, cfg.winsorize.upper_pct
    min_peers = cfg.peer_group.min_peers
    df = long.join(peers.select("company_id", "industry", "sector"), on="company_id")
    ok = pl.col("status") == "ok"
    df = df.with_columns(
        pl.col("value").filter(ok).quantile(lo, "linear").over("factor").alias("_lo"),
        pl.col("value").filter(ok).quantile(hi, "linear").over("factor").alias("_hi"))
    df = df.with_columns(pl.col("value").clip(pl.col("_lo"), pl.col("_hi")).alias("winsorized"))
    df = df.with_columns(pl.when(pl.col("higher_is_better")).then(pl.col("winsorized"))
                         .otherwise(-pl.col("winsorized")).alias("_x"))

    def stats(level: str) -> list[pl.Expr]:
        grp = ["factor", level]
        return [pl.col("_x").count().over(grp).alias(f"_n_{level}"),
                pl.col("_x").mean().over(grp).alias(f"_m_{level}"),
                pl.col("_x").std().over(grp).alias(f"_s_{level}"),
                (pl.col("_x").rank("average").over(grp) / pl.col("_x").count().over(grp))
                .alias(f"_p_{level}")]
    df = df.with_columns(*stats("industry"), *stats("sector"))
    use_ind = (pl.col("_n_industry") >= min_peers) & pl.col("industry").is_not_null()
    use_sec = (pl.col("_n_sector") >= min_peers) & pl.col("sector").is_not_null()
    z = lambda lvl: (pl.col("_x") - pl.col(f"_m_{lvl}")) / pl.col(f"_s_{lvl}")  # noqa: E731
    df = df.with_columns(
        pl.when(pl.col("_x").is_null()).then(None)
          .when(use_ind).then(pl.lit("industry")).when(use_sec).then(pl.lit("sector"))
          .otherwise(None).alias("peer_level"),
        pl.when(pl.col("_x").is_null()).then(None)
          .when(use_ind).then(pl.col("industry")).when(use_sec).then(pl.col("sector"))
          .otherwise(None).alias("peer_group"),
        pl.when(pl.col("_x").is_null()).then(None)
          .when(use_ind).then(pl.col("_n_industry")).when(use_sec).then(pl.col("_n_sector"))
          .otherwise(None).alias("peer_count"),
        pl.when(pl.col("_x").is_null()).then(None)
          .when(use_ind).then(z("industry")).when(use_sec).then(z("sector"))
          .otherwise(None).alias("z"),
        pl.when(pl.col("_x").is_null()).then(None)
          .when(use_ind).then(pl.col("_p_industry")).when(use_sec).then(pl.col("_p_sector"))
          .otherwise(None).alias("peer_percentile"))
    # A peer group whose values are all identical has no dispersion: z = 0, not NaN.
    df = df.with_columns(pl.when(pl.col("z").is_nan()).then(0.0).otherwise(pl.col("z"))
                         .alias("z"))
    df = df.with_columns(
        pl.when((pl.col("status") == "ok") & pl.col("z").is_null())
          .then(pl.lit("insufficient_peers")).otherwise(pl.col("status")).alias("status"))
    return df.drop([c for c in df.columns if c.startswith("_")])


def composite(norm: pl.DataFrame, cfg: ScoringConfig,
              dropped: set[str] | None = None) -> ScoreResult:
    """Weighted pillar and composite scores with coverage and per-factor contributions."""
    dropped = dropped or set()
    fw = {f: w for p in cfg.pillars.values() for f, w in p.factor_weights().items()
          if f not in dropped}
    pw = dict(cfg.pillar_weights)
    df = norm.filter(pl.col("factor").is_in(list(fw)))
    if df.height == 0:     # nothing to score: empty results with the usual columns
        return ScoreResult(
            factors=df.with_columns(pl.lit(None, dtype=pl.Float64).alias("contribution")),
            pillars=pl.DataFrame(schema={"company_id": pl.Int64, "pillar": pl.Utf8,
                                         "score": pl.Float64, "coverage": pl.Float64}),
            composite=pl.DataFrame(schema={"company_id": pl.Int64, "composite": pl.Float64,
                                           "coverage": pl.Float64}))
    df = df.with_columns(pl.col("factor").replace_strict(fw, return_dtype=pl.Float64)
                         .alias("fw"))
    applicable = pl.col("status") != "not_applicable"
    has_z = pl.col("z").is_not_null()
    pillars = (df.group_by("company_id", "pillar")
                 .agg((pl.col("fw").filter(applicable & has_z) * pl.col("z").filter(
                       applicable & has_z)).sum().alias("_wz"),
                      pl.col("fw").filter(applicable & has_z).sum().alias("_w_avail"),
                      pl.col("fw").filter(applicable).sum().alias("_w_appl"))
                 .with_columns((pl.col("_w_avail") / pl.col("_w_appl")).alias("coverage"))
                 .with_columns(
                     pl.when((pl.col("_w_avail") > 0) & (pl.col("coverage") >= MIN_PILLAR_COVERAGE))
                       .then(pl.col("_wz") / pl.col("_w_avail")).alias("score"),
                     pl.col("pillar").replace_strict(pw, return_dtype=pl.Float64).alias("pw")))
    comp = (pillars.group_by("company_id")
                   .agg((pl.col("pw").filter(pl.col("score").is_not_null())
                         * pl.col("score").filter(pl.col("score").is_not_null())).sum()
                        .alias("_ws"),
                        pl.col("pw").filter(pl.col("score").is_not_null()).sum().alias("coverage"))
                   .with_columns(pl.when(pl.col("coverage") >= MIN_COMPOSITE_COVERAGE)
                                 .then(pl.col("_ws") / pl.col("coverage")).alias("composite"))
                   .select("company_id", "composite", "coverage"))
    # Contribution of each factor to the composite: effective pillar weight x effective
    # factor weight x z, so contributions sum to the composite.
    eff = (pillars.join(comp.select("company_id", pl.col("coverage").alias("_ccov")),
                        on="company_id")
                  .select("company_id", "pillar",
                          pl.when(pl.col("score").is_not_null())
                            .then(pl.col("pw") / pl.col("_ccov")).alias("_pw_eff"),
                          pl.col("_w_avail")))
    factors = df.join(eff, on=["company_id", "pillar"], how="left").with_columns(
        pl.when(applicable & has_z & pl.col("_pw_eff").is_not_null())
          .then(pl.col("_pw_eff") * pl.col("fw") / pl.col("_w_avail") * pl.col("z"))
          .alias("contribution")).drop("_pw_eff", "_w_avail")
    return ScoreResult(factors=factors,
                       pillars=pillars.select("company_id", "pillar", "score", "coverage"),
                       composite=comp)
