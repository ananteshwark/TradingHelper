"""Typed loaders for the YAML files in config/.

Validation happens at load time so a bad weight or a typo in a pillar name
fails immediately instead of silently changing a score.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PILLARS = ("growth", "quality", "valuation", "momentum", "ownership")


def config_dir() -> Path:
    """Directory holding the YAML config. Override with IGS_CONFIG_DIR."""
    env = os.environ.get("IGS_CONFIG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config"


def _load_yaml(name: str, directory: Path | None = None) -> dict:
    path = (directory or config_dir()) / name
    with path.open() as fh:
        return yaml.safe_load(fh)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- universe


class MarketCapBuckets(_Strict):
    rank_window_months: int = Field(gt=0)
    large_max_rank: int = Field(gt=0)
    mid_max_rank: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> MarketCapBuckets:
        if self.mid_max_rank <= self.large_max_rank:
            raise ValueError("mid_max_rank must exceed large_max_rank")
        return self


class IncludeExclude(_Strict):
    include: list[str] = []
    exclude: list[str] = []


class UniverseConfig(_Strict):
    exchange: Literal["NSE"]
    min_market_cap_cr: float = Field(ge=0)
    min_filing_quarters: int = Field(ge=0)
    include_series: list[str]
    sme_series: list[str]
    include_sme: bool
    include_asm: bool
    include_gsm: bool
    market_cap_buckets: MarketCapBuckets
    include_buckets: list[Literal["large", "mid", "small"]]
    sectors: IncludeExclude
    industries: IncludeExclude


# --------------------------------------------------------------------------- scoring


class PillarFactors(_Strict):
    enabled: list[str]
    weights: dict[str, float] | None = None

    def factor_weights(self) -> dict[str, float]:
        if self.weights is None:
            if not self.enabled:
                return {}
            w = 1.0 / len(self.enabled)
            return {f: w for f in self.enabled}
        return dict(self.weights)


class Winsorize(_Strict):
    lower_pct: float = Field(ge=0, lt=0.5)
    upper_pct: float = Field(gt=0.5, le=1)


class PeerGroup(_Strict):
    level: Literal["basic_industry", "industry", "sector", "macro_sector"]
    fallback_level: Literal["industry", "sector", "macro_sector"]
    min_peers: int = Field(ge=2)


class ScoringConfig(_Strict):
    pillar_weights: dict[str, float]
    pillars: dict[str, PillarFactors]
    valuation_modules: dict[str, list[str]]
    winsorize: Winsorize
    peer_group: PeerGroup

    @model_validator(mode="after")
    def _check(self) -> ScoringConfig:
        if set(self.pillar_weights) != set(PILLARS):
            raise ValueError(f"pillar_weights must have exactly {PILLARS}")
        if set(self.pillars) != set(PILLARS):
            raise ValueError(f"pillars must have exactly {PILLARS}")
        if any(w < 0 for w in self.pillar_weights.values()):
            raise ValueError("pillar weights must be non-negative")
        total = sum(self.pillar_weights.values())
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(f"pillar weights sum to {total}, expected 1.0")
        for name, pillar in self.pillars.items():
            if len(set(pillar.enabled)) != len(pillar.enabled):
                raise ValueError(f"duplicate factor in pillar {name}")
            if pillar.weights is not None:
                if set(pillar.weights) != set(pillar.enabled):
                    raise ValueError(f"pillar {name}: weights keys must match enabled factors")
                s = sum(pillar.weights.values())
                if not math.isclose(s, 1.0, abs_tol=1e-9):
                    raise ValueError(f"pillar {name}: factor weights sum to {s}, expected 1.0")
        valuation = set(self.pillars["valuation"].enabled)
        for module, factors in self.valuation_modules.items():
            unknown = set(factors) - valuation
            if unknown:
                raise ValueError(f"valuation module {module} lists unknown factors {unknown}")
        for module in ("bank", "nbfc"):
            if "ev_ebitda" in self.valuation_modules.get(module, []):
                raise ValueError(f"EV/EBITDA must never apply to {module}")
        return self


# --------------------------------------------------------------------------- backtest


class Rebalance(_Strict):
    primary: Literal["monthly", "quarterly"]
    sensitivity: list[Literal["monthly", "quarterly"]] = []
    quarterly_lag_days: int = Field(ge=0)


class BacktestConfig(_Strict):
    rebalance: Rebalance
    signal_cutoff_time_ist: dt.time
    execution: Literal["next_close", "next_open"]
    forward_horizons_months: list[int]
    n_quantiles: int = Field(ge=2)
    benchmark: str
    include_delisted: bool


# --------------------------------------------------------------------------- red flags


class RedFlagsConfig(BaseModel):
    """Thresholds are kept as plain dicts per flag; each flag module validates its own."""

    model_config = ConfigDict(extra="allow", frozen=True)

    def flag(self, name: str) -> dict:
        data = getattr(self, name, None)
        if data is None:
            raise KeyError(f"red flag {name!r} not configured")
        return dict(data)


# --------------------------------------------------------------------------- sources


class SourceSpec(_Strict):
    id: str
    tier: Literal[1, 2, 3]
    description: str
    url: str | None
    kind: Literal["date_file", "date_range", "static", "per_symbol"]
    format: Literal["zip_csv", "csv", "json", "text", "xml"]
    session: Literal["none", "nse_cookie", "bse_referer"]
    probe_date: dt.date | None = None
    probe_symbol: str | None = None
    options: dict[str, Any] = {}


class SourcesConfig(_Strict):
    sources: list[SourceSpec]

    @model_validator(mode="after")
    def _unique(self) -> SourcesConfig:
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source id in sources.yaml")
        return self

    def get(self, source_id: str) -> SourceSpec:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(f"unknown source {source_id!r}")


# --------------------------------------------------------------------------- loaders


def load_universe(directory: Path | None = None) -> UniverseConfig:
    return UniverseConfig.model_validate(_load_yaml("universe.yaml", directory))


def load_scoring(directory: Path | None = None) -> ScoringConfig:
    return ScoringConfig.model_validate(_load_yaml("scoring.yaml", directory))


def load_backtest(directory: Path | None = None) -> BacktestConfig:
    return BacktestConfig.model_validate(_load_yaml("backtest.yaml", directory))


def load_red_flags(directory: Path | None = None) -> RedFlagsConfig:
    return RedFlagsConfig.model_validate(_load_yaml("red_flags.yaml", directory))


def load_sources(directory: Path | None = None) -> SourcesConfig:
    return SourcesConfig.model_validate(_load_yaml("sources.yaml", directory))
