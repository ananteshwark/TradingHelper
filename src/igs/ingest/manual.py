"""Tier 3 inputs: user-supplied Screener.in exports and the yfinance price fallback.

Both are landed verbatim in the raw store like everything else. Neither ever
feeds point-in-time fundamentals or overrides a Tier 1 price.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from igs.dq import DQLog
from igs.ingest.raw_store import RawStore
from igs.normalize.load import load_simple
from igs.normalize.masters import (
    parse_screener_csv,
    parse_screener_excel,
    parse_screener_workbook,
)


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


@dataclass
class ScreenerImport:
    """What one Screener.in export added."""
    file: str
    rows: int = 0
    company_id: int | None = None
    symbol: str | None = None
    name: str | None = None              # as the export names the company
    fetch_id: str | None = None
    already: bool = False                # the same file was imported before

    def __str__(self) -> str:
        who = f"{self.name or 'a screen'}" + (f" ({self.symbol})" if self.symbol else "")
        if self.already:
            return f"{self.file}: already imported ({who})"
        return f"{self.file}: {self.rows} values for {who}"


def _symbol(conn, company_id: int) -> str | None:
    row = conn.execute("""select si.id_value from security_identifier si
                          join security s using (security_id)
                          where s.company_id = %s and si.id_type = 'NSE_SYMBOL'
                          order by si.valid_to is null desc, si.valid_from desc limit 1""",
                       (company_id,)).fetchone()
    return row[0] if row else None


def import_screener_bytes(conn, store: RawStore, content: bytes, filename: str, dq: DQLog,
                          nse_code: str | None = None, bse_code: str | None = None
                          ) -> ScreenerImport:
    """Land and load one Screener.in export the owner downloaded: a screen's CSV (many
    companies, by their NSE/BSE code columns) or a company's "Export to Excel" workbook.
    The workbook's company is the NSE or BSE code given, else the one company its name
    matches; it is refused rather than guessed. The same file twice is loaded once."""
    out = ScreenerImport(file=filename)
    sha = hashlib.sha256(content).hexdigest()
    seen = conn.execute("""select p.fetch_id, e.company_id from raw_payload p
                           join screener_enrichment e on e.source_fetch_id = p.fetch_id
                           where p.source_id = 'screener_export' and p.content_sha256 = %s
                           limit 1""", (sha,)).fetchone()
    excel = filename.lower().endswith((".xlsx", ".xlsm"))
    if excel:
        book = parse_screener_workbook(content)
        out.name = book["company_name"]
    if seen:
        out.already, out.fetch_id, out.company_id = True, seen[0], seen[1]
        out.symbol = _symbol(conn, seen[1]) if seen[1] else None
        return out
    if excel:
        from igs.brokers import match_company
        cid = (_company_by_code(conn, nse_code, bse_code) if nse_code or bse_code
               else match_company(conn, out.name or Path(filename).stem))
        if cid is None:
            raise ValueError(
                f"{filename}: no single company matches "
                + (f"code {nse_code or bse_code}" if nse_code or bse_code
                   else f"the name {out.name or Path(filename).stem!r}")
                + "; import it from the stock's page, or with --nse SYMBOL")
        out.company_id, out.symbol = cid, _symbol(conn, cid)
        df = parse_screener_excel(content, out.symbol, bse_code)
    else:
        df = parse_screener_csv(content)
    rec = store.put(source_id="screener_export", content=content, url=None, http_status=None,
                    origin="manual", request_params={"original_filename": filename},
                    note="user upload (tier 3)")
    store.index_record(conn, rec)
    out.fetch_id = rec.fetch_id
    ids = {}
    for nse, bse in df.select("nse_code", "bse_code").unique().iter_rows():
        ids[(nse, bse)] = out.company_id if excel else _company_by_code(conn, nse, bse)
        if ids[(nse, bse)] is None:
            dq.emit("warn", "screener_unmapped", f"Screener row {nse or bse} matches no company",
                    fetch_id=rec.fetch_id)
    df = df.with_columns(pl.struct("nse_code", "bse_code").map_elements(
        lambda s: ids[(s["nse_code"], s["bse_code"])], return_dtype=pl.Int64).alias("company_id"))
    with conn.transaction(), conn.cursor() as cur:
        with cur.copy("copy screener_enrichment (source_fetch_id, nse_code, bse_code, company_id, "
                      "section, field, period_label, value_text, value_num) from stdin") as cp:
            for r in df.iter_rows(named=True):
                cp.write_row((rec.fetch_id, r["nse_code"], r["bse_code"], r["company_id"],
                              r["section"], r["field"], r["period_label"], r["value_text"],
                              r["value_num"]))
    out.rows = df.height
    return out


def import_screener(conn, store: RawStore, path: Path, dq: DQLog,
                    nse_code: str | None = None, bse_code: str | None = None
                    ) -> ScreenerImport:
    """`import_screener_bytes` for a file on disk."""
    return import_screener_bytes(conn, store, Path(path).read_bytes(), Path(path).name, dq,
                                 nse_code, bse_code)


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
