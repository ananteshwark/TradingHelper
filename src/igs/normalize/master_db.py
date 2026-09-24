"""Persist the instrument master built from bhavcopy observations.

Company and security ids are stable across rebuilds because both are upserted
on natural keys (issuer code, first ISIN of the chain). Identifier ranges are
derived data and are replaced wholesale on each rebuild.
"""

from __future__ import annotations

import polars as pl
import psycopg

from igs.dq import DQLog
from igs.normalize.instrument_master import identifier_ranges, identifier_spans, link_securities
from igs.normalize.isin import issuer_code

INACTIVE_AFTER_DAYS = 30


def _latest_names(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("""select distinct on (isin) isin, company_name from nse_equity_list
                       order by isin, snapshot_date desc""")
        return dict(cur.fetchall())


def rebuild_instrument_master(conn: psycopg.Connection, dq: DQLog) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("select trade_date, isin, symbol from price_eod where exchange = 'NSE'")
        rows = cur.fetchall()
    if not rows:
        dq.emit("warn", "master_no_prices", "no NSE prices loaded; instrument master is empty")
        return {"securities": 0}
    obs = pl.DataFrame(rows, schema={"trade_date": pl.Date, "isin": pl.Utf8, "symbol": pl.Utf8},
                       orient="row")
    spans = identifier_spans(obs)
    links = link_securities(spans, dq)
    last_date = obs["trade_date"].max()
    ranges = identifier_ranges(spans, links, last_date)
    names = _latest_names(conn)
    latest_symbol = (spans.join(links, on="isin").sort("last_seen")
                          .group_by("security_key").agg(pl.col("symbol").last(),
                                                        pl.col("isin").last().alias("last_isin"),
                                                        pl.col("last_seen").max()))

    with conn.cursor() as cur:
        sec_ids: dict[str, int] = {}
        for r in latest_symbol.iter_rows(named=True):
            key = r["security_key"]
            name = names.get(r["last_isin"]) or names.get(key) or r["symbol"]
            cur.execute("""insert into company (name, company_key) values (%s, %s)
                           on conflict (company_key) do update set name = excluded.name
                           returning company_id""", (name, issuer_code(key)))
            company_id = cur.fetchone()[0]
            cur.execute("""insert into security (company_id, security_key) values (%s, %s)
                           on conflict (security_key) do update set company_id = excluded.company_id
                           returning security_id""", (company_id, key))
            sec_ids[key] = cur.fetchone()[0]

        cur.execute("delete from security_identifier where id_type in ('ISIN', 'NSE_SYMBOL')")
        cur.executemany(
            """insert into security_identifier (security_id, id_type, id_value, valid_from,
                                                valid_to, evidence)
               values (%s, %s, %s, %s, %s, 'nse_bhavcopy')""",
            [(sec_ids[r["security_key"]], r["id_type"], r["id_value"], r["valid_from"],
              r["valid_to"]) for r in ranges.iter_rows(named=True)])

        # BSE codes and broker tokens hang off ISIN / current NSE symbol.
        cur.execute("delete from security_identifier where id_type = 'BSE_CODE'")
        cur.execute("""
            insert into security_identifier (security_id, id_type, id_value, valid_from, evidence,
                                             source_fetch_id)
            select distinct on (si.security_id) si.security_id, 'BSE_CODE', b.scrip_code,
                   b.first_seen, 'bse_scrip_isin', b.source_fetch_id
            from (select scrip_code, isin, min(snapshot_date) as first_seen,
                         max(source_fetch_id) as source_fetch_id
                  from bse_scrip where isin is not null group by scrip_code, isin) b
            join security_identifier si on si.id_type = 'ISIN' and si.id_value = b.isin
            order by si.security_id, b.first_seen desc
            on conflict do nothing""")
        cur.execute("delete from security_identifier where id_type = 'ANGEL_TOKEN_NSE'")
        cur.execute("""
            insert into security_identifier (security_id, id_type, id_value, valid_from, evidence,
                                             source_fetch_id)
            select distinct on (si.security_id) si.security_id, 'ANGEL_TOKEN_NSE', b.token,
                   b.snapshot_date, 'angel_master_symbol', b.source_fetch_id
            from (select distinct on (token) token, symbol, snapshot_date, source_fetch_id
                  from broker_instrument where broker = 'angel' and exchange = 'NSE'
                  order by token, snapshot_date desc) b
            join security_identifier si on si.id_type = 'NSE_SYMBOL' and si.id_value = b.symbol
                 and si.valid_to is null
            order by si.security_id, b.snapshot_date desc
            on conflict do nothing""")

        # Listing status: active if it traded recently; delisted if it has left EQUITY_L.
        cur.execute("delete from security_listing where exchange = 'NSE'")
        cur.execute("select max(snapshot_date) from nse_equity_list")
        latest_list = cur.fetchone()[0]
        listed_isins = set()
        if latest_list:
            cur.execute("select isin from nse_equity_list where snapshot_date = %s", (latest_list,))
            listed_isins = {r[0] for r in cur.fetchall()}
        listing_rows = []
        for r in latest_symbol.iter_rows(named=True):
            stale = (last_date - r["last_seen"]).days > INACTIVE_AFTER_DAYS
            if not stale:
                status = "active"
            elif latest_list and r["last_isin"] not in listed_isins:
                status = "delisted"
            else:
                status = "suspended"
            listing_rows.append((sec_ids[r["security_key"]], status,
                                 r["last_seen"] if status == "delisted" else None))
        cur.executemany("""insert into security_listing (security_id, exchange, status,
                                                         delisted_on)
                           values (%s, 'NSE', %s, %s)""", listing_rows)
        cur.execute("""update security_listing sl set listed_on = e.listed_on
                       from (select distinct on (isin) isin, listed_on from nse_equity_list
                             order by isin, snapshot_date desc) e
                       join security_identifier si on si.id_type = 'ISIN' and si.id_value = e.isin
                       where sl.security_id = si.security_id and sl.exchange = 'NSE'""")

        # Attach corporate actions to securities by the symbol valid the day before ex-date.
        cur.execute("""update corporate_action ca set security_id = si.security_id
                       from security_identifier si
                       where ca.exchange = 'NSE' and si.id_type = 'NSE_SYMBOL'
                         and si.id_value = ca.symbol
                         and ca.ex_date - 1 >= si.valid_from
                         and (si.valid_to is null or ca.ex_date - 1 < si.valid_to)""")
        cur.execute("select count(*) from corporate_action where security_id is null")
        unmapped = cur.fetchone()[0]
    if unmapped:
        dq.emit("warn", "ca_unmapped", f"{unmapped} corporate actions match no security")
    return {"securities": len(sec_ids), "identifier_ranges": ranges.height,
            "unmapped_corporate_actions": unmapped}
