"""Instrument master: dated ISIN / symbol ranges per security.

Built from what the exchange actually printed each day (bhavcopy rows), so it
is reproducible from raw data:

1. `identifier_spans` - contiguous runs of (ISIN, symbol) as observed.
2. `link_securities`  - group spans into securities:
     * same ISIN, new symbol            -> rename, same security
     * same symbol, new ISIN, same issue prefix, short gap
                                        -> ISIN change (e.g. FV split), same security
     * same symbol, new ISIN, different issuer
                                        -> symbol reused by another company: NOT linked,
                                           reported as a data-quality warning
3. `identifier_ranges` - valid_from / valid_to (exclusive) per identifier.

A security's range for an identifier ends where its successor begins; a
security that stopped trading gets valid_to = last day seen + 1.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from igs.dq import DQLog
from igs.normalize.isin import is_valid_isin, issue_prefix

MAX_ISIN_CHANGE_GAP_DAYS = 10


def identifier_spans(obs: pl.DataFrame) -> pl.DataFrame:
    """obs: trade_date, isin, symbol (one row per day per line, series already filtered).

    Returns isin, symbol, first_seen, last_seen, days_seen.
    """
    daily = obs.select("trade_date", "isin", "symbol").unique().sort("isin", "trade_date")
    return (daily.with_columns(
                (pl.col("symbol") != pl.col("symbol").shift(1).over("isin"))
                .fill_null(True).cum_sum().over("isin").alias("_run"))
            .group_by("isin", "_run")
            .agg(pl.col("symbol").first(),
                 pl.col("trade_date").min().alias("first_seen"),
                 pl.col("trade_date").max().alias("last_seen"),
                 pl.len().alias("days_seen"))
            .drop("_run")
            .sort("isin", "first_seen"))


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def link_securities(spans: pl.DataFrame, dq: DQLog) -> pl.DataFrame:
    """Assign a security_key to every ISIN. Returns isin, security_key, link_evidence."""
    uf = _UnionFind()
    evidence: dict[str, str] = {}
    for isin in spans["isin"].unique().sort():
        uf.find(isin)
        if not is_valid_isin(isin):
            dq.emit("warn", "invalid_isin", f"ISIN {isin} fails check-digit validation",
                    details={"isin": isin})

    by_symbol = spans.sort("symbol", "first_seen").partition_by("symbol", as_dict=False)
    for group in by_symbol:
        rows = group.to_dicts()
        for prev, cur in zip(rows, rows[1:], strict=False):
            if prev["isin"] == cur["isin"]:
                continue
            gap = (cur["first_seen"] - prev["last_seen"]).days
            same_issue = issue_prefix(prev["isin"]) == issue_prefix(cur["isin"])
            if same_issue and 0 < gap <= MAX_ISIN_CHANGE_GAP_DAYS:
                uf.union(prev["isin"], cur["isin"])
                evidence[cur["isin"]] = f"isin_change_from:{prev['isin']}"
                dq.emit("info", "isin_change",
                        f"{cur['symbol']}: ISIN {prev['isin']} -> {cur['isin']} linked",
                        details={"symbol": cur["symbol"], "gap_days": gap})
            elif same_issue:
                dq.emit("warn", "isin_change_unlinked",
                        f"{cur['symbol']}: ISIN {prev['isin']} -> {cur['isin']} same issuer but "
                        f"gap {gap}d outside (0, {MAX_ISIN_CHANGE_GAP_DAYS}]; needs review",
                        details={"symbol": cur["symbol"], "gap_days": gap})
            else:
                dq.emit("warn", "symbol_reused",
                        f"symbol {cur['symbol']} moved from {prev['isin']} to unrelated issuer "
                        f"{cur['isin']}; treated as different securities",
                        details={"symbol": cur["symbol"], "gap_days": gap})

    isins = spans["isin"].unique().sort().to_list()
    return pl.DataFrame({
        "isin": isins,
        "security_key": [uf.find(i) for i in isins],
        "link_evidence": [evidence.get(i, "observed") for i in isins],
    })


def _ranges(df: pl.DataFrame, value_col: str, id_type: str, last_date: dt.date) -> pl.DataFrame:
    """Collapse runs of one identifier per security into [valid_from, valid_to) ranges."""
    runs = (df.sort("security_key", "first_seen")
              .with_columns((pl.col(value_col) != pl.col(value_col).shift(1).over("security_key"))
                            .fill_null(True).cum_sum().over("security_key").alias("_run"))
              .group_by("security_key", "_run")
              .agg(pl.col(value_col).first().alias("id_value"),
                   pl.col("first_seen").min().alias("valid_from"),
                   pl.col("last_seen").max().alias("last_seen"))
              .sort("security_key", "valid_from"))
    runs = runs.with_columns(pl.col("valid_from").shift(-1).over("security_key").alias("_next"))
    return runs.select(
        "security_key",
        pl.lit(id_type).alias("id_type"),
        "id_value",
        "valid_from",
        pl.when(pl.col("_next").is_not_null()).then(pl.col("_next"))
          .when(pl.col("last_seen") >= last_date).then(None)
          .otherwise(pl.col("last_seen") + dt.timedelta(days=1))
          .alias("valid_to"),
    )


def identifier_ranges(spans: pl.DataFrame, links: pl.DataFrame,
                      last_date: dt.date) -> pl.DataFrame:
    """ISIN and NSE_SYMBOL ranges per security_key.

    last_date is the last date covered by the observations; identifiers still
    seen on it are open-ended (valid_to null).
    """
    s = spans.join(links, on="isin")
    isin = _ranges(s, "isin", "ISIN", last_date)
    sym = _ranges(s, "symbol", "NSE_SYMBOL", last_date)
    return pl.concat([isin, sym]).sort("security_key", "id_type", "valid_from")


def build_master(obs: pl.DataFrame, dq: DQLog) -> pl.DataFrame:
    spans = identifier_spans(obs)
    links = link_securities(spans, dq)
    return identifier_ranges(spans, links, obs["trade_date"].max())
