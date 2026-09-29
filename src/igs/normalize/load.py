"""Write normalised frames into PostgreSQL.

Loaders are idempotent: replaying the same raw payload twice leaves the
tables unchanged, which is what makes `igs rebuild` (truncate + replay every
landed payload in order) safe.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import polars as pl
import psycopg

from igs.dq import DQLog


def _copy_upsert(conn: psycopg.Connection, table: str, df: pl.DataFrame, cols: Sequence[str],
                 conflict: Sequence[str], update: Sequence[str] = ()) -> int:
    """COPY rows into a temp table, then INSERT ... ON CONFLICT. Returns rows written."""
    if df.height == 0:
        return 0
    tmp = f"_tmp_{table}"
    col_list = ", ".join(cols)
    with conn.cursor() as cur:
        cur.execute(f"create temp table if not exists {tmp} (like {table} including defaults) "
                    "on commit drop")
        cur.execute(f"truncate {tmp}")
        with cur.copy(f"copy {tmp} ({col_list}) from stdin") as cp:
            for row in df.select(cols).iter_rows():
                cp.write_row(row)
        action = "do nothing"
        if update:
            sets = ", ".join(f"{c} = excluded.{c}" for c in update)
            differs = " or ".join(f"{table}.{c} is distinct from excluded.{c}" for c in update)
            action = f"do update set {sets} where {differs}"
        cur.execute(f"insert into {table} ({col_list}) select {col_list} from {tmp} "
                    f"on conflict ({', '.join(conflict)}) {action}")
        return cur.rowcount


PRICE_COLS = ["exchange", "trade_date", "isin", "symbol", "series", "open", "high", "low", "close",
              "last", "prev_close", "volume", "turnover_inr", "trades", "session_id",
              "source_fetch_id"]


def load_prices(conn, df: pl.DataFrame, fetch_id: str, dq: DQLog) -> int:
    df = df.filter(pl.col("close").is_not_null()).with_columns(
        pl.lit(fetch_id).alias("source_fetch_id"))
    with conn.cursor() as cur:
        cur.execute("select count(*) from price_eod "
                    "where exchange = 'NSE' and trade_date = any(%s)",
                    (df["trade_date"].unique().to_list(),))
        existing = cur.fetchone()[0]
    n = _copy_upsert(conn, "price_eod", df, PRICE_COLS,
                     ["exchange", "trade_date", "isin", "series"],
                     update=[c for c in PRICE_COLS if c not in
                             ("exchange", "trade_date", "isin", "series", "source_fetch_id")])
    if existing and n:
        dq.emit("warn", "bhavcopy_reissued",
                f"{n} existing price rows changed by a later fetch; latest fetch kept",
                fetch_id=fetch_id)
    return n


def load_delivery(conn, df: pl.DataFrame) -> int:
    if df.height == 0:
        return 0
    with conn.cursor() as cur:
        cur.execute("create temp table if not exists _tmp_deliv (trade_date date, symbol text, "
                    "series text, delivery_qty bigint, delivery_pct numeric) on commit drop")
        cur.execute("truncate _tmp_deliv")
        with cur.copy("copy _tmp_deliv from stdin") as cp:
            for r in df.select("trade_date", "symbol", "series", "delivery_qty",
                               "delivery_pct").iter_rows():
                cp.write_row(r)
        cur.execute("""update price_eod p set delivery_qty = d.delivery_qty,
                              delivery_pct = d.delivery_pct
                       from _tmp_deliv d
                       where p.exchange = 'NSE' and p.trade_date = d.trade_date
                         and p.symbol = d.symbol and p.series = d.series""")
        return cur.rowcount


CA_COLS = ["exchange", "symbol", "isin", "action_type", "ex_date", "record_date", "announced_at",
           "fv_old", "fv_new", "ratio_a", "ratio_b", "issue_price", "cash_per_share", "subject",
           "source_fetch_id"]


def load_corporate_actions(conn, df: pl.DataFrame, fetch_id: str) -> int:
    df = df.with_columns(pl.lit(fetch_id).alias("source_fetch_id"))
    return _copy_upsert(conn, "corporate_action", df, CA_COLS,
                        ["exchange", "symbol", "ex_date", "action_type", "subject"])


def load_simple(conn, table: str, df: pl.DataFrame, fetch_id: str, conflict: Sequence[str],
                extra: dict | None = None) -> int:
    df = df.with_columns(pl.lit(fetch_id).alias("source_fetch_id"),
                         *[pl.lit(v).alias(k) for k, v in (extra or {}).items()])
    return _copy_upsert(conn, table, df, df.columns, conflict)


def load_insider_trades(conn, df: pl.DataFrame, fetch_id: str, ingested_at: dt.datetime) -> int:
    df = df.with_columns(pl.lit(fetch_id).alias("source_fetch_id"),
                         pl.lit(ingested_at).alias("ingested_at"))
    return _copy_upsert(conn, "insider_trade", df, df.columns,
                        ["exchange", "symbol", "person_name", "filed_at", "side", "quantity",
                         "trade_from"])


def load_announcements(conn, df: pl.DataFrame, fetch_id: str, ingested_at: dt.datetime) -> int:
    df = df.with_columns(pl.lit(fetch_id).alias("source_fetch_id"),
                         pl.lit(ingested_at).alias("ingested_at"))
    return _copy_upsert(conn, "announcement", df, df.columns,
                        ["exchange", "symbol", "filed_at", "subject"])
