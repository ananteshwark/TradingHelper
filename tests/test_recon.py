from __future__ import annotations

import datetime as dt

import polars as pl
from synthetic import business_days, price_frame, standard_dataset

from igs.dq import DQLog
from igs.normalize.adjust import adjusted_prices, event_factors
from igs.recon import checks
from igs.recon.report import render
from igs.timeutil import utc_now


def _nse(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(pl.lit("NSE").alias("exchange"), pl.lit("EQ").alias("series"),
                           pl.format("ISIN{}", pl.col("security_id")).alias("isin"),
                           pl.format("SYM{}", pl.col("security_id")).alias("symbol"))


def test_unique_price_rows():
    px = _nse(price_frame(1, {dt.date(2024, 1, 1): 10.0, dt.date(2024, 1, 2): 11.0}))
    assert checks.unique_price_rows(px).status == "pass"
    dup = pl.concat([px, px.head(1)])
    res = checks.unique_price_rows(dup)
    assert res.status == "fail" and res.details.height == 1


def test_session_filter():
    raw = pl.DataFrame({"isin": ["A", "A", "A"], "SsnId": ["I1", "F1", "F1"]})
    kept = raw.filter(pl.col("SsnId") == "F1")
    assert checks.session_filter(raw, kept, "SsnId", ["F1"]).status == "pass"
    assert checks.session_filter(raw, raw, "SsnId", ["F1"]).status == "fail"


def test_calendar_coverage():
    days = business_days(dt.date(2024, 1, 1), dt.date(2024, 1, 5))
    px = price_frame(1, {d: 1.0 for d in days[:-1]})
    res = checks.calendar_coverage(px, days)
    assert res.status == "fail" and res.details["trade_date"].to_list() == [days[-1]]
    assert checks.calendar_coverage(px, days[:-1]).status == "pass"


def test_isin_mapping():
    px = _nse(price_frame(1, {dt.date(2024, 1, 1): 1.0, dt.date(2024, 1, 2): 1.0}))
    ident = pl.DataFrame({"security_key": ["k"], "id_type": ["ISIN"], "id_value": ["ISIN1"],
                          "valid_from": [dt.date(2024, 1, 1)], "valid_to": [None]},
                         schema_overrides={"valid_to": pl.Date})
    assert checks.isin_mapping(px, ident).status == "pass"
    ended = ident.with_columns(pl.lit(dt.date(2024, 1, 2)).alias("valid_to"))
    res = checks.isin_mapping(px, ended)
    assert res.status == "fail" and res.details["trade_date"].to_list() == [dt.date(2024, 1, 2)]


def test_symbol_consistency():
    a = _nse(price_frame(1, {dt.date(2024, 1, 1): 1.0}))
    b = a.with_columns(pl.lit("OTHERISIN").alias("isin"))
    assert checks.symbol_consistency(a).status == "pass"
    assert checks.symbol_consistency(pl.concat([a, b])).status == "fail"


def _exchange_style_prices() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Security 2 from the standard dataset with prev_close reset on ex-dates, as the
    exchange does."""
    ds = standard_dataset()
    px = ds.tables["prices"].filter(pl.col("security_id") == 2).drop("known_at")
    cas = ds.tables["corporate_actions"].drop("known_at")
    fixed = px.with_columns(
        pl.when(pl.col("trade_date") == dt.date(2024, 3, 15)).then(pl.col("prev_close") * 0.5)
          .when(pl.col("trade_date") == dt.date(2024, 6, 14)).then(pl.col("prev_close") * 0.2)
          .otherwise(pl.col("prev_close")).alias("prev_close"))
    return fixed, cas


def test_factors_agree_with_exchange():
    px, cas = _exchange_style_prices()
    res = checks.factors_vs_exchange(px, event_factors(cas, px))
    assert res.status == "pass", res.details


def test_missing_corporate_action_is_caught():
    px, cas = _exchange_style_prices()
    only_bonus = cas.filter(pl.col("action_type") == "bonus")
    res = checks.factors_vs_exchange(px, event_factors(only_bonus, px))
    assert res.status == "fail"
    assert res.details.filter(pl.col("verdict") == "missing_corporate_action")[
        "trade_date"].to_list() == [dt.date(2024, 6, 14)]


def test_wrong_ratio_is_caught():
    px, cas = _exchange_style_prices()
    wrong = cas.with_columns(pl.when(pl.col("action_type") == "split").then(1.0)
                             .otherwise(pl.col("fv_new")).alias("fv_new"))  # 10 -> 1
    res = checks.factors_vs_exchange(px, event_factors(wrong, px))
    assert res.status == "fail"
    assert "factor_mismatch" in res.details["verdict"].to_list()


def test_unexplained_gaps():
    px, cas = _exchange_style_prices()
    f = event_factors(cas, px)
    adj = adjusted_prices(px, f, dt.date(2024, 9, 30))
    assert checks.unexplained_gaps(adj, f).status == "pass"
    # Without the split on record, the split day shows up as an unexplained 80% drop.
    f2 = event_factors(cas.filter(pl.col("action_type") == "bonus"), px)
    res = checks.unexplained_gaps(adjusted_prices(px, f2, dt.date(2024, 9, 30)), f2)
    assert res.status == "warn" and res.details["trade_date"].to_list() == [dt.date(2024, 6, 14)]


def test_cross_source_close():
    a = _nse(price_frame(1, {dt.date(2024, 1, d): 100.0 for d in range(1, 6)}))
    b = a.with_columns(pl.col("close") * 1.0005)          # 5 bps
    assert checks.cross_source_close(a, b).status == "pass"
    c = a.with_columns(pl.col("close") * 1.01)            # 100 bps
    assert checks.cross_source_close(a, c).status == "fail"


def test_report_renders_failures_and_dq():
    dq = DQLog()
    dq.emit("warn", "symbol_reused", "XYZ reused")
    px = _nse(price_frame(1, {dt.date(2024, 1, 1): 1.0}))
    results = [checks.unique_price_rows(pl.concat([px, px])), checks.symbol_consistency(px)]
    md = render(results, dq, "Reconciliation", utc_now())
    assert "overall: **FAIL**" in md
    assert "## unique_price_rows" in md and "XYZ reused" in md
