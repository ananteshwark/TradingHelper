"""Schema-level guarantees, checked against a real PostgreSQL."""

from __future__ import annotations

import datetime as dt

import polars as pl
import psycopg
import pytest
from synthetic import standard_dataset

from igs.db import MigrationError, migrate
from igs.pit import facts_as_of

pytestmark = pytest.mark.db


def _raw(cur, fetch_id="s__20240101T000000000000Z__abc"):
    cur.execute(
        """insert into raw_payload (fetch_id, source_id, fetched_at, content_sha256, size_bytes,
                                    blob_path, origin)
           values (%s, 's', now(), repeat('a', 64), 1, 'blobs/aa/a', 'http')""", (fetch_id,))
    return fetch_id


def _company_and_filing(cur, fetch_id, filed_at="2024-02-12 17:00+05:30", sha="b"):
    cur.execute("insert into company (name) values ('Co') returning company_id")
    cid = cur.fetchone()[0]
    cur.execute(
        """insert into filing (company_id, exchange, filing_system, filing_type, filed_at,
                               ingested_at, content_sha256, source_fetch_id)
           values (%s, 'NSE', 'test', 'financial_results', %s, now(), repeat(%s, 64), %s)
           returning filing_id""", (cid, filed_at, sha, fetch_id))
    return cid, cur.fetchone()[0]


def test_migrations_are_idempotent(db_conn):
    assert migrate(db_conn) == []


def test_edited_migration_is_refused(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("update schema_migrations set sha256 = repeat('0', 64) "
                    "where version = '001_raw_and_dq'")
    db_conn.commit()
    with pytest.raises(MigrationError, match="changed after being applied"):
        migrate(db_conn)


def test_fundamentals_are_append_only(db_conn):
    with db_conn.cursor() as cur:
        fid = _raw(cur)
        cid, filing_id = _company_and_filing(cur, fid)
        cur.execute(
            """insert into fundamental_fact (filing_id, company_id, statement_basis, period_end,
                   period_type, concept, source_element, value, unit, filed_at, ingested_at)
               values (%s, %s, 'consolidated', '2023-06-30', 'Q', 'revenue', 'in-capmkt:X',
                       100, 'INR', '2024-02-12 17:00+05:30', now())""", (filing_id, cid))
    db_conn.commit()
    for stmt in ("update fundamental_fact set value = 1", "delete from fundamental_fact",
                 "update filing set filed_at = now()", "delete from raw_payload"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with db_conn.cursor() as cur:
                cur.execute(stmt)
        db_conn.rollback()
    with db_conn.cursor() as cur:            # rebuilds use TRUNCATE
        cur.execute("truncate fundamental_fact")
        cur.execute("select count(*) from fundamental_fact")
        assert cur.fetchone()[0] == 0


def test_identifier_ranges_cannot_overlap(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("insert into company (name) values ('A') returning company_id")
        c = cur.fetchone()[0]
        cur.execute("insert into security (company_id) values (%s), (%s) returning security_id",
                    (c, c))
        s1, s2 = (r[0] for r in cur.fetchall())
        ins = ("insert into security_identifier (security_id, id_type, id_value, valid_from, "
               "valid_to, evidence) values (%s, %s, %s, %s, %s, 'test')")
        cur.execute(ins, (s1, "NSE_SYMBOL", "XYZ", dt.date(2010, 1, 1), dt.date(2015, 1, 1)))
        # Reuse of the symbol after the first range closes is fine.
        cur.execute(ins, (s2, "NSE_SYMBOL", "XYZ", dt.date(2015, 1, 1), None))
    db_conn.commit()
    with pytest.raises(psycopg.errors.ExclusionViolation):
        with db_conn.cursor() as cur:
            cur.execute(ins, (s1, "NSE_SYMBOL", "XYZ", dt.date(2014, 6, 1), dt.date(2016, 1, 1)))
    db_conn.rollback()
    with pytest.raises(psycopg.errors.ExclusionViolation):
        with db_conn.cursor() as cur:   # one security, two symbols at once
            cur.execute(ins, (s2, "NSE_SYMBOL", "ABC", dt.date(2020, 1, 1), None))
    db_conn.rollback()


def test_master_filters_non_equity_series_and_keeps_sme_history(db_conn):
    from igs.dq import DQLog
    from igs.normalize.master_db import rebuild_instrument_master
    from igs.xbrl.load import resolve_company

    # Real conflicting symbol/ISIN pairs from the failed live rebuild.
    rows = [
        ("2024-09-02", "IMC1", "INE00QS24019", "N0"),
        ("2024-09-02", "IMC1", "INE00QS24027", "N2"),
        ("2024-09-02", "RADIOCITY", "INE919I01024", "EQ"),
        ("2024-09-02", "RADIOCITY", "INE919I04010", "P1"),
        ("2024-09-02", "SHAREINDIA", "INE932X01026", "EQ"),
        ("2024-09-02", "SHAREINDIA", "INE932X13013", "W1"),
        ("2024-09-02", "SASKEN", "INE231F01020", "SM"),
        ("2024-09-03", "SASKEN", "INE231F01020", "ST"),
        ("2024-09-04", "SASKEN", "INE231F01020", "EQ"),
        ("2024-09-05", "SASKEN", "INE231F01020", "BE"),
    ]
    with db_conn.cursor() as cur:
        fid = _raw(cur)
        cur.executemany("""insert into price_eod
                           (exchange, trade_date, symbol, isin, series, close, source_fetch_id)
                           values ('NSE', %s, %s, %s, %s, 100, %s)""",
                        [(*r, fid) for r in rows])
    db_conn.commit()
    for _ in range(2):  # Rebuilding must preserve identity and remain constraint-safe.
        with db_conn.transaction():
            stats = rebuild_instrument_master(db_conn, DQLog())
        assert stats["securities"] == 3
        with db_conn.cursor() as cur:
            cur.execute("""select id_value, valid_from, valid_to from security_identifier
                           where id_type = 'NSE_SYMBOL' order by id_value""")
            assert cur.fetchall() == [
                ("RADIOCITY", dt.date(2024, 9, 2), dt.date(2024, 9, 3)),
                ("SASKEN", dt.date(2024, 9, 2), None),
                ("SHAREINDIA", dt.date(2024, 9, 2), dt.date(2024, 9, 3)),
            ]
            cur.execute("select count(*) from price_eod")
            assert cur.fetchone()[0] == len(rows)
        assert resolve_company(db_conn, "SASKEN", dt.date(2024, 9, 2)) is not None


def _load_standard_facts(db_conn) -> pl.DataFrame:
    facts = standard_dataset().tables["facts"]
    with db_conn.cursor() as cur:
        fid = _raw(cur)
        ids = {}
        for n, cid in enumerate(sorted(facts["company_id"].unique().to_list())):
            cur.execute("insert into company (company_id, name) values (%s, 'c')", (cid,))
            cur.execute(
                """insert into filing (company_id, exchange, filing_system, filing_type, filed_at,
                       ingested_at, content_sha256, source_fetch_id)
                   values (%s, 'NSE', 't', 'r', now(), now(), repeat(%s, 64), %s)
                   returning filing_id""", (cid, str(n), fid))
            ids[cid] = cur.fetchone()[0]
        for r in facts.iter_rows(named=True):
            cur.execute(
                """insert into fundamental_fact (fact_id, filing_id, company_id, statement_basis,
                       period_end, period_type, concept, source_element, value, unit, filed_at,
                       ingested_at)
                   values (%s, %s, %s, %s, %s, %s, %s, 'x', %s, 'INR', %s, now())""",
                (r["fact_id"], ids[r["company_id"]], r["company_id"], r["statement_basis"],
                 r["period_end"], r["period_type"], r["concept"], r["value"], r["filed_at"]))
    db_conn.commit()
    return facts


def test_sql_and_polars_point_in_time_agree(db_conn):
    facts = _load_standard_facts(db_conn)
    for as_of in (dt.datetime(2023, 11, 14, 10, tzinfo=dt.UTC),
                  dt.datetime(2023, 11, 14, 13, tzinfo=dt.UTC),
                  dt.datetime(2024, 2, 1, tzinfo=dt.UTC),
                  dt.datetime(2024, 3, 1, tzinfo=dt.UTC)):
        with db_conn.cursor() as cur:
            cur.execute("select fact_id from facts_as_of(%s) order by fact_id", (as_of,))
            sql_ids = [r[0] for r in cur.fetchall()]
        assert sql_ids == sorted(facts_as_of(facts, as_of)["fact_id"].to_list())


def test_versions_count_only_changed_values(db_conn):
    _load_standard_facts(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("""select value, version, is_restatement from fundamental_fact_versioned
                       where company_id = 1 and period_end = '2023-06-30' order by filed_at""")
        assert [(float(v), n, r) for v, n, r in cur.fetchall()] == [(100.0, 1, False),
                                                                    (90.0, 2, True)]


def test_db_status_reports_what_is_loaded(db_conn, monkeypatch, capsys):
    """`igs db status`: the latest price day and filing, and rows per table, without
    printing the password."""
    import os

    import db_market

    from igs import cli
    db_market.load(db_conn)
    url = os.environ["IGS_TEST_DATABASE_URL"]
    monkeypatch.setenv("IGS_DATABASE_URL", url)
    assert cli._db_status(cli.build_parser().parse_args(["db", "status"])) == 0
    out = capsys.readouterr().out
    with db_conn.cursor() as cur:
        cur.execute("select max(trade_date) from price_eod")
        latest = cur.fetchone()[0]
    assert f"latest price    {latest}" in out and "none loaded" not in out
    assert "price_eod" in out and url.split("@")[0] not in out
