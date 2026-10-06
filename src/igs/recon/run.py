"""Run the step 1 reconciliation against the database and write the report."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl

from igs.dq import DQLog
from igs.normalize.adjust import adjusted_prices, event_factors
from igs.recon import checks
from igs.recon.checks import CheckResult
from igs.recon.report import write
from igs.timeutil import utc_now


def _frame(conn, sql: str, params: tuple, schema: dict) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return pl.DataFrame(rows, schema=schema, orient="row")


def load_inputs(conn, start: dt.date, end: dt.date, series: list[str]) -> dict[str, pl.DataFrame]:
    prices = _frame(conn, """
        select p.exchange, p.trade_date, p.isin, p.symbol, p.series, p.close::float8,
               p.prev_close::float8, p.volume, si.security_id
        from price_eod p
        left join security_identifier si
          on si.id_type = 'ISIN' and si.id_value = p.isin and p.trade_date >= si.valid_from
         and (si.valid_to is null or p.trade_date < si.valid_to)
        where p.exchange = 'NSE' and p.trade_date between %s and %s and p.series = any(%s)""",
        (start, end, series),
        {"exchange": pl.Utf8, "trade_date": pl.Date, "isin": pl.Utf8, "symbol": pl.Utf8,
         "series": pl.Utf8, "close": pl.Float64, "prev_close": pl.Float64, "volume": pl.Int64,
         "security_id": pl.Int64})
    identifiers = _frame(conn, """
        select security_id::text, id_type, id_value, valid_from, valid_to
        from security_identifier where id_type in ('ISIN', 'NSE_SYMBOL')""", (),
        {"security_key": pl.Utf8, "id_type": pl.Utf8, "id_value": pl.Utf8,
         "valid_from": pl.Date, "valid_to": pl.Date})
    actions = _frame(conn, """
        select ca_id, security_id, action_type, ex_date, fv_old::float8, fv_new::float8,
               ratio_a::float8, ratio_b::float8, issue_price::float8, cash_per_share::float8
        from corporate_action
        where security_id is not null and ex_date between %s and %s""", (start, end),
        {"ca_id": pl.Int64, "security_id": pl.Int64, "action_type": pl.Utf8, "ex_date": pl.Date,
         "fv_old": pl.Float64, "fv_new": pl.Float64, "ratio_a": pl.Float64, "ratio_b": pl.Float64,
         "issue_price": pl.Float64, "cash_per_share": pl.Float64})
    # Whole calendar years: a window with no holiday in it is still covered by a
    # calendar that was loaded for that year.
    holidays = _frame(conn, "select holiday_date from trading_holiday where exchange = 'NSE' "
                      "and extract(year from holiday_date) between %s and %s",
                      (start.year, end.year), {"holiday_date": pl.Date})
    fallback = _frame(conn, """
        select f.trade_date, si.id_value as isin, f.close::float8
        from price_eod_fallback f
        join security_identifier nse on nse.id_type = 'NSE_SYMBOL' and nse.id_value = f.symbol
         and f.trade_date >= nse.valid_from
         and (nse.valid_to is null or f.trade_date < nse.valid_to)
        join security_identifier si on si.security_id = nse.security_id and si.id_type = 'ISIN'
         and f.trade_date >= si.valid_from and (si.valid_to is null or f.trade_date < si.valid_to)
        where f.trade_date between %s and %s""", (start, end),
        {"trade_date": pl.Date, "isin": pl.Utf8, "close": pl.Float64})
    return {"prices": prices, "identifiers": identifiers, "actions": actions,
            "holidays": holidays, "fallback": fallback}


def expected_trading_days(start: dt.date, end: dt.date, holidays: set[dt.date]) -> list[dt.date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def run_checks(inputs: dict[str, pl.DataFrame], start: dt.date, end: dt.date,
               dq: DQLog) -> list[CheckResult]:
    prices = inputs["prices"]
    results = [checks.unique_price_rows(prices), checks.symbol_consistency(prices),
               checks.isin_mapping(prices, inputs["identifiers"])]
    if inputs["holidays"].height:
        results.append(checks.calendar_coverage(
            prices, expected_trading_days(start, end, set(inputs["holidays"]["holiday_date"]))))
    else:
        dq.emit("warn", "no_holiday_calendar",
                "trading holiday calendar not loaded; calendar coverage check skipped")
    mapped = prices.filter(pl.col("security_id").is_not_null())
    factors = event_factors(inputs["actions"], mapped)
    results.append(checks.factors_vs_exchange(mapped, factors))
    results.append(checks.unexplained_gaps(adjusted_prices(mapped, factors, end), factors))
    if inputs["fallback"].height:
        results.append(checks.cross_source_close(prices, inputs["fallback"]))
    else:
        dq.emit("info", "no_cross_source",
                "no second price source loaded; cross-source check skipped")
    return results


def run_reconciliation(conn, start: dt.date, end: dt.date, out_dir: Path,
                       series: list[str] | None = None,
                       dq: DQLog | None = None) -> tuple[list[CheckResult], Path]:
    dq = dq or DQLog()
    inputs = load_inputs(conn, start, end, series or ["EQ", "BE"])
    results = run_checks(inputs, start, end, dq)
    path = write(results, dq, out_dir / f"reconciliation_{start}_{end}.md",
                 f"Reconciliation {start} to {end}", utc_now())
    return results, path
