"""Point-in-time dataset and view.

`PitDataset` holds every time-stamped table with its `known_at` column.
`PitView(dataset, as_of)` is the only way factor code reaches that data: each
accessor filters on known_at <= as_of first, and the view's audit records the
latest known_at it handed out so a run can prove it saw nothing from the
future.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import polars as pl

from igs.normalize.adjust import adjusted_prices, event_factors
from igs.pit.knowledge import KNOWN_AT, with_known_at
from igs.timeutil import UTC, ist_date, require_aware

FACT_KEY = ["company_id", "statement_basis", "period_end", "period_type", "concept"]
EMPTY_FACTS = {"fact_id": pl.Int64, "company_id": pl.Int64, "statement_basis": pl.Utf8,
               "period_end": pl.Date, "period_type": pl.Utf8, "concept": pl.Utf8,
               "value": pl.Float64, "filed_at": pl.Datetime("us", "UTC")}


class LookAheadError(AssertionError):
    """Raised when a computation at as_of used data that was not public at as_of."""


def latest_versions(facts: pl.DataFrame) -> pl.DataFrame:
    """Latest filed value per fact key. Polars twin of SQL facts_as_of()."""
    return (facts.sort([*FACT_KEY, "filed_at", "fact_id"])
                 .unique(subset=FACT_KEY, keep="last", maintain_order=True))


def facts_as_of(facts: pl.DataFrame, as_of: dt.datetime) -> pl.DataFrame:
    """Latest version of each fact with filed_at <= as_of (never period_end <= as_of)."""
    as_of = require_aware(as_of, "as_of").astimezone(UTC)
    return latest_versions(facts.filter(pl.col("filed_at") <= as_of))


@dataclass(frozen=True)
class PitDataset:
    tables: dict[str, pl.DataFrame]

    @classmethod
    def from_frames(cls, **frames: pl.DataFrame) -> PitDataset:
        return cls({name: with_known_at(name, df) for name, df in frames.items()})

    def truncate(self, as_of: dt.datetime) -> PitDataset:
        """Physically drop every row not yet known at as_of."""
        as_of = require_aware(as_of, "as_of").astimezone(UTC)
        return PitDataset({n: df.filter(pl.col(KNOWN_AT) <= as_of)
                           for n, df in self.tables.items()})

    def poison_future(self, as_of: dt.datetime) -> PitDataset:
        """Corrupt every float value in rows not yet known at as_of.

        A computation that is truly point-in-time gives identical results on the
        poisoned dataset.
        """
        as_of = require_aware(as_of, "as_of").astimezone(UTC)
        out = {}
        for name, df in self.tables.items():
            future = pl.col(KNOWN_AT) > as_of
            float_cols = [c for c, t in df.schema.items() if t in (pl.Float32, pl.Float64)]
            out[name] = df.with_columns(
                [pl.when(future).then(pl.col(c) * 1000.0 + 7.0).otherwise(pl.col(c)).alias(c)
                 for c in float_cols]
            )
        return PitDataset(out)


@dataclass
class AccessAudit:
    as_of: dt.datetime
    accessed: list[tuple[str, int, dt.datetime | None]] = field(default_factory=list)

    def record(self, table: str, df: pl.DataFrame, time_col: str = KNOWN_AT) -> None:
        latest = df[time_col].max() if df.height else None
        self.accessed.append((table, df.height, latest))

    def violations(self) -> list[tuple[str, int, dt.datetime]]:
        return [(t, n, ts) for t, n, ts in self.accessed if ts is not None and ts > self.as_of]

    def assert_clean(self) -> None:
        bad = self.violations()
        if bad:
            raise LookAheadError(f"as_of {self.as_of.isoformat()}: accessed future rows {bad}")


class PitView:
    def __init__(self, dataset: PitDataset, as_of: dt.datetime) -> None:
        self.as_of = require_aware(as_of, "as_of").astimezone(UTC)
        self.as_of_date = ist_date(self.as_of)
        self._data = dataset
        self.audit = AccessAudit(self.as_of)
        self._memo: dict[str, object] = {}

    def memo(self, key: str, build):
        """Cache a derived frame for this view only (one as_of; never shared across dates)."""
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    def has(self, table: str) -> bool:
        return table in self._data.tables

    def table(self, name: str) -> pl.DataFrame:
        """All rows of a table known at as_of."""
        df = self._data.tables[name].filter(pl.col(KNOWN_AT) <= self.as_of)
        self.audit.record(name, df)
        return df

    def facts(
        self,
        concepts: list[str] | None = None,
        company_ids: list[int] | None = None,
        statement_basis: str | None = None,
    ) -> pl.DataFrame:
        """Latest known version of each fact (empty when the dataset has no facts)."""
        if not self.has("facts"):
            return pl.DataFrame(schema=EMPTY_FACTS)
        df = self.table("facts")
        if concepts is not None:
            df = df.filter(pl.col("concept").is_in(concepts))
        if company_ids is not None:
            df = df.filter(pl.col("company_id").is_in(company_ids))
        if statement_basis is not None:
            df = df.filter(pl.col("statement_basis") == statement_basis)
        out = latest_versions(df)
        self.audit.record("facts[out]", out, "filed_at")
        return out

    def prices(self, security_ids: list[int] | None = None, adjusted: bool = True) -> pl.DataFrame:
        """EOD prices up to as_of; adjusted only for actions with ex_date <= as_of date."""
        px = self.table("prices")
        if security_ids is not None:
            px = px.filter(pl.col("security_id").is_in(security_ids))
        if not adjusted:
            return px
        cas = self.table("corporate_actions") if self.has("corporate_actions") else None
        if cas is None or cas.height == 0:
            return adjusted_prices(px, _empty_factors(), self.as_of_date)
        factors = event_factors(cas, px)
        return adjusted_prices(px, factors, self.as_of_date)


def _empty_factors() -> pl.DataFrame:
    return pl.DataFrame(schema={"security_id": pl.Int64, "ca_id": pl.Int64, "ex_date": pl.Date,
                                "action_type": pl.Utf8, "factor": pl.Float64,
                                "status": pl.Utf8})
