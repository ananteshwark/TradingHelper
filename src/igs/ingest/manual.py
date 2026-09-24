"""Tier 3 inputs: user-supplied Screener.in exports and the yfinance price fallback.

Both are landed verbatim in the raw store like everything else. Neither ever
feeds point-in-time fundamentals or overrides a Tier 1 price.
"""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import polars as pl

from igs.dq import DQLog
from igs.ingest.raw_store import RawStore
from igs.normalize.load import load_simple
from igs.normalize.masters import parse_screener_csv, parse_screener_excel


def _company_by_code(conn, nse: str | None, bse: str | None) -> int | None:
    with conn.cursor() as cur:
        for id_type, code in (("NSE_SYMBOL", nse), ("BSE_CODE", bse)):
            if not code:
                continue
            cur.execute("""select s.company_id from security_identifier si
                           join security s using (security_id)
                           where si.id_type = %s and si.id_value = %s
                           order by si.valid_to is null desc, si.valid_from desc limit 1""",
                        (id_type, code))
            row = cur.fetchone()
            if row:
                return row[0]
    return None


def import_screener(conn, store: RawStore, path: Path, dq: DQLog,
                    nse_code: str | None = None, bse_code: str | None = None) -> int:
    """Land and load a Screener.in CSV (screen) or Excel (single company) export."""
    rec = store.put_file(source_id="screener_export", path=path, note="user upload (tier 3)")
    store.index_record(conn, rec)
    content = store.read_bytes(rec)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        if not (nse_code or bse_code):
            raise ValueError("an Excel export covers one company: pass its NSE or BSE code")
        df = parse_screener_excel(content, nse_code, bse_code)
    else:
        df = parse_screener_csv(content)
    ids = {}
    for nse, bse in df.select("nse_code", "bse_code").unique().iter_rows():
        ids[(nse, bse)] = _company_by_code(conn, nse, bse)
        if ids[(nse, bse)] is None:
            dq.emit("warn", "screener_unmapped", f"Screener row {nse or bse} matches no company",
                    fetch_id=rec.fetch_id)
    df = df.with_columns(pl.struct("nse_code", "bse_code").map_elements(
        lambda s: ids[(s["nse_code"], s["bse_code"])], return_dtype=pl.Int64).alias("company_id"))
    with conn.transaction(), conn.cursor() as cur:
        with cur.copy("copy screener_enrichment (source_fetch_id, nse_code, bse_code, company_id, "
                      "field, period_label, value_text, value_num) from stdin") as cp:
            for r in df.iter_rows(named=True):
                cp.write_row((rec.fetch_id, r["nse_code"], r["bse_code"], r["company_id"],
                              r["field"], r["period_label"], r["value_text"], r["value_num"]))
    return df.height


def import_yfinance(conn, store: RawStore, symbols: list[str], start: dt.date,
                    end: dt.date) -> int:
    """Fallback price history. Flagged unverified everywhere it is shown.

    Requires the optional `yfinance` package; NSE tickers use the '.NS' suffix.
    """
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - optional
        raise RuntimeError("yfinance is not installed (pip install yfinance)") from exc
    total = 0
    for sym in symbols:
        hist = yf.Ticker(f"{sym}.NS").history(start=start.isoformat(),
                                               end=(end + dt.timedelta(days=1)).isoformat(),
                                               auto_adjust=False)
        buf = io.StringIO()
        hist.to_csv(buf)
        rec = store.put(source_id="yfinance_fallback", content=buf.getvalue().encode(),
                        url=f"yfinance:{sym}.NS", http_status=200,
                        request_params={"symbol": sym, "start": start.isoformat(),
                                        "end": end.isoformat()},
                        note="yfinance library output (tier 3, unverified)")
        store.index_record(conn, rec)
        df = pl.read_csv(io.StringIO(buf.getvalue()), try_parse_dates=True)
        if df.height == 0:
            continue
        out = pl.DataFrame({
            "provider": "yfinance", "symbol": sym,
            "trade_date": df["Date"].cast(pl.Utf8).str.slice(0, 10).str.to_date(),
            "close": df["Close"], "adj_close": df.get_column("Adj Close"),
            "volume": df["Volume"].cast(pl.Int64)})
        total += load_simple(conn, "price_eod_fallback", out, rec.fetch_id,
                             ["provider", "symbol", "trade_date"])
    conn.commit()
    return total
