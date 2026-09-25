"""Load the synthetic market into PostgreSQL through the real tables, so scoring,
persistence, the service layer, the API and alerts can be tested from the
database outwards."""

from __future__ import annotations

import datetime as dt

import polars as pl
import synthetic_market as M

SYMBOLS = {1: "GROW", 2: "CYCL", 3: "BANK", 4: "NBFC", 5: "GAPS", 6: "LATE"}
NAMES = {1: "Grow Industries Ltd", 2: "Cyclical Steel Ltd", 3: "Example Bank Ltd",
         4: "Example Finance Ltd", 5: "Gaps Engineering Ltd", 6: "Late Foods Ltd"}
FETCH = "synthetic__20240101T000000000000Z__000000000000"


def isin(cid: int) -> str:
    return f"INE{cid:03d}X01010"


def load(conn) -> None:
    ds = M.build()
    t = {k: v.drop("known_at") for k, v in ds.tables.items()}
    with conn.cursor() as cur:
        cur.execute("""insert into raw_payload (fetch_id, source_id, fetched_at, content_sha256,
                           size_bytes, blob_path, origin)
                       values (%s, 'synthetic', now(), repeat('0', 64), 0, 'x', 'manual')""",
                    (FETCH,))
        for cid in range(1, 7):
            cur.execute("insert into company (company_id, name, company_key) values (%s, %s, %s)",
                        (cid, NAMES[cid], f"K{cid}"))
            cur.execute("insert into security (security_id, company_id, security_key) "
                        "values (%s, %s, %s)", (100 + cid, cid, isin(cid)))
            for id_type, value in (("ISIN", isin(cid)), ("NSE_SYMBOL", SYMBOLS[cid])):
                cur.execute("""insert into security_identifier (security_id, id_type,
                                   id_value, valid_from, evidence)
                               values (%s, %s, %s, '2017-01-01', 'test')""",
                            (100 + cid, id_type, value))
            ind = M.INDUSTRY[cid]
            cur.execute("""insert into industry_classification (company_id, macro_sector, sector,
                               industry, basic_industry, valid_from)
                           values (%s, 'X', 'X', %s, %s, '2017-01-01')""", (cid, ind, ind))
        px = t["prices"].with_columns(
            pl.col("company_id").map_elements(isin, return_dtype=pl.Utf8).alias("isin"),
            pl.col("company_id").replace_strict(SYMBOLS, return_dtype=pl.Utf8).alias("symbol"))
        with cur.copy("""copy price_eod (exchange, trade_date, isin, symbol, series, open, high,
                             low, close, prev_close, volume, delivery_pct, source_fetch_id)
                         from stdin""") as cp:
            for r in px.iter_rows(named=True):
                cp.write_row(("NSE", r["trade_date"], r["isin"], r["symbol"], "EQ", r["open"],
                              r["high"], r["low"], r["close"], r["prev_close"], r["volume"],
                              r["delivery_pct"], FETCH))
        for r in t["corporate_actions"].iter_rows(named=True):
            cid = r["security_id"] - 100
            cur.execute("""insert into corporate_action (exchange, symbol, isin, security_id,
                               action_type, ex_date, announced_at, fv_old, fv_new, ratio_a,
                               ratio_b, subject, source_fetch_id)
                           values ('NSE', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (SYMBOLS[cid], isin(cid), r["security_id"], r["action_type"],
                         r["ex_date"], r["announced_at"], r["fv_old"], r["fv_new"], r["ratio_a"],
                         r["ratio_b"], r["action_type"], FETCH))
        facts = t["facts"]
        filings = facts.group_by("filing_id").agg(pl.col("company_id").first(),
                                                  pl.col("period_end").max(),
                                                  pl.col("filed_at").first())
        with cur.copy("""copy filing (filing_id, company_id, exchange, filing_system, filing_type,
                             period_end, statement_basis, filed_at, ingested_at, content_sha256,
                             source_fetch_id, source_url) from stdin""") as cp:
            for r in filings.iter_rows(named=True):
                cp.write_row((r["filing_id"], r["company_id"], "NSE", "synthetic",
                              "financial_results", r["period_end"], "consolidated",
                              r["filed_at"], r["filed_at"], f"{r['filing_id']:064d}", FETCH,
                              f"https://example.invalid/results/{r['filing_id']}.xml"))
        with cur.copy("""copy fundamental_fact (fact_id, filing_id, company_id, statement_basis,
                             period_end, period_type, concept, source_element, value, unit,
                             filed_at, ingested_at) from stdin""") as cp:
            for r in facts.iter_rows(named=True):
                cp.write_row((r["fact_id"], r["filing_id"], r["company_id"], r["statement_basis"],
                              r["period_end"], r["period_type"], r["concept"], "synthetic",
                              r["value"], "INR", r["filed_at"], r["filed_at"]))
        shp = t["shareholding"]
        shp_filings = shp.group_by("filing_id").agg(pl.col("company_id").first(),
                                                    pl.col("period_end").first(),
                                                    pl.col("filed_at").first())
        with cur.copy("""copy filing (filing_id, company_id, exchange, filing_system, filing_type,
                             period_end, filed_at, ingested_at, content_sha256, source_fetch_id)
                         from stdin""") as cp:
            for r in shp_filings.iter_rows(named=True):
                cp.write_row((r["filing_id"], r["company_id"], "NSE", "synthetic_shp",
                              "shareholding", r["period_end"], r["filed_at"], r["filed_at"],
                              f"{r['filing_id']:064d}", FETCH))
        with cur.copy("""copy shareholding (filing_id, company_id, period_end, category, shares,
                             pct_of_total, pledged_pct, holders, filed_at, ingested_at)
                         from stdin""") as cp:
            for r in shp.iter_rows(named=True):
                cp.write_row((r["filing_id"], r["company_id"], r["period_end"], r["category"],
                              r["shares"], r["pct_of_total"], r["pledged_pct"], r["holders"],
                              r["filed_at"], r["filed_at"]))
        with cur.copy("copy index_price (index_name, trade_date, close, is_total_return, "
                      "source_fetch_id) from stdin") as cp:
            for r in t["index_prices"].iter_rows(named=True):
                cp.write_row((r["index_name"], r["trade_date"], r["close"], False, FETCH))
        for tbl in ("company", "security", "filing", "fundamental_fact"):
            key = {"company": "company_id", "security": "security_id",
                   "filing": "filing_id", "fundamental_fact": "fact_id"}[tbl]
            cur.execute(f"select setval(pg_get_serial_sequence('{tbl}', '{key}'), "
                        f"(select max({key}) from {tbl}))")
        # An announcement so the resignation flag has something to find.
        cur.execute("""insert into announcement (exchange, symbol, filed_at, category, subject,
                           source_fetch_id, ingested_at)
                       values ('NSE', 'LATE', '2024-10-15 18:00+05:30',
                               'Resignation of Chief Financial Officer',
                               'Resignation of Mr A as Chief Financial Officer', %s, now())""",
                    (FETCH,))
        for r in t["insider_trades"].iter_rows(named=True):
            cur.execute("""insert into insider_trade (exchange, symbol, person_name,
                               person_category, insider_role, security_type, transaction_type,
                               acquisition_mode, side, open_market, quantity, value_inr,
                               trade_from, filed_at, source_fetch_id, ingested_at)
                           values ('NSE', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                   %s, now())""",
                        (r["symbol"], r["person_name"], r["insider_role"].replace("_", " "),
                         r["insider_role"], r["security_type"], r["side"].title(),
                         "Market Purchase" if r["open_market"] else "ESOP", r["side"],
                         r["open_market"], r["quantity"], r["value_inr"], r["trade_from"],
                         r["filed_at"], FETCH))
        cur.execute("""insert into surveillance_snapshot (measure, list_name, symbol, stage,
                           effective_from, source_fetch_id)
                       values ('ASM', 'longterm', 'GAPS', 'Stage I', '2024-11-01', %s),
                              ('GSM', 'gsm', 'NOTLISTED', 'Stage 0', '2024-11-01', %s)""",
                    (FETCH, FETCH))
    conn.commit()


AS_OF = dt.datetime(2024, 11, 29, 23, 59, tzinfo=M.IST)
