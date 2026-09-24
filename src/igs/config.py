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
    with path.open(encoding="utf-8") as fh:
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


class Tiers(_Strict):
    high_conviction_top_pct: float = Field(gt=0, le=100)
    watchlist_top_pct: float = Field(gt=0, le=100)
    high_conviction_min_coverage: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _order(self) -> Tiers:
        if self.watchlist_top_pct < self.high_conviction_top_pct:
            raise ValueError("watchlist_top_pct must be >= high_conviction_top_pct")
        return self


class Robustness(_Strict):
    enabled: bool = True
    weight_draws: int = Field(ge=10)
    weight_concentration: float = Field(gt=0)
    seed: int
    min_weight_stability: float = Field(ge=0, le=1)
    persistence_months: int = Field(ge=0)
    persistence_top_pct: float = Field(gt=0, le=100)
    min_persistence: int = Field(ge=0)
    min_positive_pillars: int = Field(ge=0, le=5)
    min_pillar_score: float
    max_factor_share: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _persistence(self) -> Robustness:
        if self.min_persistence > self.persistence_months:
            raise ValueError("min_persistence cannot exceed persistence_months")
        return self


class RunHealth(_Strict):
    enabled: bool = True
    max_price_age_days: int = Field(ge=0)
    min_universe_traded_share: float = Field(ge=0, le=1)
    max_announcement_age_days: int = Field(ge=0)
    max_surveillance_age_days: int = Field(ge=0)
    max_results_age_days: int = Field(ge=0)
    drift_max_gap_days: int = Field(ge=0)
    max_universe_change: float = Field(ge=0)
    max_coverage_drop: float = Field(ge=0, le=1)
    max_factor_ok_drop: float = Field(ge=0, le=1)
    min_top_decile_overlap: float = Field(ge=0, le=1)


class ScoringConfig(_Strict):
    pillar_weights: dict[str, float]
    pillars: dict[str, PillarFactors]
    valuation_modules: dict[str, list[str]]
    winsorize: Winsorize
    peer_group: PeerGroup
    tiers: Tiers
    respect_ic_status: bool
    robustness: Robustness
    run_health: RunHealth
    plausibility: dict[str, tuple[float, float]] = {}

    @model_validator(mode="after")
    def _check(self) -> ScoringConfig:
        enabled_all = {f for p in self.pillars.values() for f in p.enabled}
        for name, (lo, hi) in self.plausibility.items():
            if name not in enabled_all:
                raise ValueError(f"plausibility bound for unknown factor {name}")
            if lo >= hi:
                raise ValueError(f"plausibility bound for {name}: min must be below max")
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


class IcGate(_Strict):
    primary_horizon_months: int
    min_abs_t: float = Field(ge=0)
    min_observations: int = Field(ge=2)


class PortfolioSpec(_Strict):
    quantile: Literal["top"]
    weighting: Literal["equal"]


class FailureSpec(_Strict):
    horizon_months: int = Field(gt=0)
    max_loss_pct: float = Field(gt=0, le=100)
    max_drawdown_pct: float = Field(gt=0, le=100)
    count_stopped_trading: bool
    max_underperformance_pp: float | None = None
    effectiveness_top_pct: float = Field(gt=0, le=100)
    min_tripped_for_verdict: int = Field(ge=1)


class BacktestConfig(_Strict):
    rebalance: Rebalance
    signal_cutoff_time_ist: dt.time
    execution: Literal["next_close", "next_open"]
    forward_horizons_months: list[int]
    n_quantiles: int = Field(ge=2)
    benchmark: str
    include_delisted: bool
    ic_gate: IcGate
    walk_forward_ic_selection: bool
    portfolio: PortfolioSpec
    failure: FailureSpec

    @model_validator(mode="after")
    def _horizon(self) -> BacktestConfig:
        if self.ic_gate.primary_horizon_months not in self.forward_horizons_months:
            raise ValueError("ic_gate.primary_horizon_months must be a forward horizon")
        return self


class Statutory(_Strict):
    stt_buy_pct: float
    stt_sell_pct: float
    exchange_txn_pct: float
    sebi_fee_pct: float
    stamp_duty_buy_pct: float
    gst_pct: float
    dp_charge_inr_per_sell: float


class Brokerage(_Strict):
    pct: float = Field(ge=0)
    max_inr_per_order: float = Field(ge=0)


class Impact(_Strict):
    half_spread_bps: dict[Literal["large", "mid", "small"], float]
    sqrt_coefficient: float = Field(ge=0)
    max_bps: float = Field(gt=0)
    illiquid_participation: float = Field(gt=0)


class CostsConfig(_Strict):
    statutory: Statutory
    brokerage: Brokerage
    impact: Impact
    capital_inr: float = Field(gt=0)


# --------------------------------------------------------------------------- red flags


class RedFlagsConfig(BaseModel):
    """Thresholds are kept as plain dicts per flag; each flag module validates its own."""

    model_config = ConfigDict(extra="allow", frozen=True)

    @model_validator(mode="after")
    def _entries(self) -> RedFlagsConfig:
        for name, entry in (self.model_extra or {}).items():
            if not isinstance(entry, dict):
                raise ValueError(f"red flag {name}: expected a mapping")
            if entry.get("severity", "reject") not in ("reject", "caution"):
                raise ValueError(f"red flag {name}: severity must be reject or caution")
            for key in ("enabled", "unavailable_blocks"):
                if key in entry and not isinstance(entry[key], bool):
                    raise ValueError(f"red flag {name}: {key} must be true or false")
        return self

    def names(self) -> list[str]:
        return list(self.model_extra or {})

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
    kind: Literal["date_file", "date_range", "static", "per_symbol", "paged"]
    format: Literal["zip_csv", "csv", "json", "text", "xml"]
    session: Literal["none", "nse_cookie", "bse_referer"]
    probe_date: dt.date | None = None
    probe_symbol: str | None = None
    options: dict[str, Any] = {}

    @model_validator(mode="after")
    def _paging(self) -> SourceSpec:
        need = {"first_page", "page_size", "max_pages"}
        if self.kind == "paged" and not need <= set(self.options):
            raise ValueError(f"{self.id}: a paged source needs options {sorted(need)}")
        if self.kind == "paged" and self.url and not ("{page}" in self.url
                                                      and "{size}" in self.url):
            raise ValueError(f"{self.id}: a paged url needs {{page}} and {{size}}")
        return self


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


class AlertsConfig(BaseModel):
    """rules.<name> is a dict with at least `enabled`; channels enable email/telegram."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    rules: dict[str, dict[str, Any]]
    channels: dict[Literal["email", "telegram"], bool]


def load_alerts(directory: Path | None = None) -> AlertsConfig:
    return AlertsConfig.model_validate(_load_yaml("alerts.yaml", directory))


def load_costs(directory: Path | None = None) -> CostsConfig:
    return CostsConfig.model_validate(_load_yaml("costs.yaml", directory))


def load_red_flags(directory: Path | None = None) -> RedFlagsConfig:
    return RedFlagsConfig.model_validate(_load_yaml("red_flags.yaml", directory))


def load_sources(directory: Path | None = None) -> SourcesConfig:
    return SourcesConfig.model_validate(_load_yaml("sources.yaml", directory))


# --------------------------------------------------------------------------- assistant

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AskFeature(_Strict):
    effort: Effort = "high"
    max_tokens: int = Field(16000, ge=1024)
    max_tool_rounds: int = Field(8, ge=1, le=30)


class BriefFeature(_Strict):
    effort: Effort = "medium"
    max_tokens: int = Field(8000, ge=1024)


class AnnouncementsFeature(_Strict):
    effort: Effort = "low"
    max_tokens: int = Field(8000, ge=1024)
    days: int = Field(7, ge=1, le=90)
    max_per_run: int = Field(100, ge=1)
    batch_size: int = Field(10, ge=1, le=25)
    scope: Literal["universe", "watchlist"] = "universe"


class AssistantFeatures(_Strict):
    ask: AskFeature = AskFeature()
    brief: BriefFeature = BriefFeature()
    announcements: AnnouncementsFeature = AnnouncementsFeature()


class TokenPrice(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class AssistantConfig(_Strict):
    """Optional LLM research assistant (config/assistant.yaml). Never used by scoring."""

    enabled: bool = False
    model: str = "claude-opus-5"
    fallbacks: Literal["default"] | None = "default"
    daily_budget_usd: float = Field(2.0, ge=0)
    features: AssistantFeatures = AssistantFeatures()
    prices_usd_per_mtok: dict[str, TokenPrice] = {}

    @model_validator(mode="after")
    def _priced(self) -> AssistantConfig:
        if self.model not in self.prices_usd_per_mtok:
            raise ValueError(f"no price for {self.model} in prices_usd_per_mtok: the daily "
                             "budget cannot be enforced without one")
        return self


def settings_dir() -> Path:
    """Local, untracked settings written by the UI's Settings page (data/settings by
    default; IGS_SETTINGS_DIR to move it)."""
    env = os.environ.get("IGS_SETTINGS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data" / "settings"


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) \
            else v
    return out


def assistant_overrides() -> dict:
    path = settings_dir() / "assistant.yaml"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_assistant(directory: Path | None = None) -> AssistantConfig:
    """config/assistant.yaml, with any values changed on the Settings page on top."""
    return AssistantConfig.model_validate(
        deep_merge(_load_yaml("assistant.yaml", directory), assistant_overrides()))
