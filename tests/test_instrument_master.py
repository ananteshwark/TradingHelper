from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from igs.dq import DQLog
from igs.normalize.instrument_master import build_master, identifier_spans, link_securities
from igs.normalize.isin import is_valid_isin, issue_prefix


def with_check_digit(first11: str) -> str:
    for d in "0123456789":
        if is_valid_isin(first11 + d):
            return first11 + d
    raise AssertionError(first11)


@pytest.mark.parametrize("isin", ["US0378331005", "AU0000XVGZA3", "GB0002634946",
                                  "INE002A01018", "INE009A01021"])
def test_valid_isins(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize("isin", ["US0378331006", "INE002A0101", "ine002a01018", "", None])
def test_invalid_isins(isin):
    assert not is_valid_isin(isin)


OLD = with_check_digit("INE123A0101")   # same issuer, issue serial 01
NEW = with_check_digit("INE123A0102")   # ... serial 02 after a face-value split
OTHER = with_check_digit("INE999Z0101")  # unrelated issuer


def obs(rows: list[tuple[str, str, str, str]]) -> pl.DataFrame:
    """rows: (first_date, last_date, isin, symbol) expanded to weekdays."""
    out = []
    for start, end, isin, sym in rows:
        d, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        while d <= e:
            if d.weekday() < 5:
                out.append({"trade_date": d, "isin": isin, "symbol": sym})
            d += dt.timedelta(days=1)
    return pl.DataFrame(out)


def ranges(master: pl.DataFrame, id_type: str) -> list[tuple]:
    return [(r["id_value"], r["valid_from"].isoformat(),
             r["valid_to"].isoformat() if r["valid_to"] else None)
            for r in master.filter(pl.col("id_type") == id_type).sort("valid_from").to_dicts()]


def test_issue_prefix_matches_across_split():
    assert issue_prefix(OLD) == issue_prefix(NEW) != issue_prefix(OTHER)


def test_symbol_rename_keeps_one_security():
    dq = DQLog()
    m = build_master(obs([("2024-01-01", "2024-03-29", OLD, "OLDNAME"),
                          ("2024-04-01", "2024-06-28", OLD, "NEWNAME")]), dq)
    assert m["security_key"].n_unique() == 1
    assert ranges(m, "NSE_SYMBOL") == [("OLDNAME", "2024-01-01", "2024-04-01"),
                                       ("NEWNAME", "2024-04-01", None)]
    assert ranges(m, "ISIN") == [(OLD, "2024-01-01", None)]


def test_isin_change_after_split_is_linked():
    dq = DQLog()
    m = build_master(obs([("2024-01-01", "2024-03-14", OLD, "ACME"),
                          ("2024-03-15", "2024-06-28", NEW, "ACME")]), dq)
    assert m["security_key"].n_unique() == 1
    assert ranges(m, "ISIN") == [(OLD, "2024-01-01", "2024-03-15"), (NEW, "2024-03-15", None)]
    assert ranges(m, "NSE_SYMBOL") == [("ACME", "2024-01-01", None)]
    assert [i.category for i in dq.issues] == ["isin_change"]


def test_symbol_reused_by_other_issuer_is_not_linked():
    dq = DQLog()
    m = build_master(obs([("2020-01-01", "2020-06-30", OLD, "XYZ"),
                          ("2023-01-02", "2024-06-28", OTHER, "XYZ")]), dq)
    assert m["security_key"].n_unique() == 2
    assert ranges(m, "NSE_SYMBOL") == [("XYZ", "2020-01-01", "2020-07-01"),
                                       ("XYZ", "2023-01-02", None)]
    assert "symbol_reused" in [i.category for i in dq.issues]


def test_long_gap_isin_change_needs_review():
    dq = DQLog()
    spans = identifier_spans(obs([("2024-01-01", "2024-01-31", OLD, "ACME"),
                                  ("2024-06-03", "2024-06-28", NEW, "ACME")]))
    links = link_securities(spans, dq)
    assert links["security_key"].n_unique() == 2
    assert [i.category for i in dq.issues] == ["isin_change_unlinked"]


def test_delisted_security_range_is_closed():
    dq = DQLog()
    m = build_master(obs([("2024-01-01", "2024-02-29", OLD, "GONE"),
                          ("2024-01-01", "2024-06-28", OTHER, "STAYS")]), dq)
    assert ("GONE", "2024-01-01", "2024-03-01") in ranges(m, "NSE_SYMBOL")
    assert ("STAYS", "2024-01-01", None) in ranges(m, "NSE_SYMBOL")


def test_invalid_isin_is_reported():
    bad = OLD[:-1] + str((int(OLD[-1]) + 1) % 10)
    dq = DQLog()
    link_securities(identifier_spans(obs([("2024-01-01", "2024-01-05", bad, "BAD")])), dq)
    assert [i.category for i in dq.issues] == ["invalid_isin"]
