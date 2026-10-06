"""Payload builders in the exchanges' DOCUMENTED formats.

These are not captured payloads. They exist so the parsers and the whole
ingest -> master -> reconcile -> rebuild pipeline can be exercised end to end
before real data is reachable. When a source is verified, its real sample is
added under tests/fixtures/real/ and parsed in test_real_payloads.py.

The mini market (NSE, EQ series):
  ACME   face-value split 10 -> 2, ex 2024-07-10, with a new ISIN from the ex-date
  BETA   1:1 bonus, ex 2024-07-11, ISIN unchanged
  OLDG   renamed to GAMMA from 2024-07-12, ISIN unchanged
Every UDiFF file also carries pre-open (I1) rows that must be dropped.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile

from igs.normalize.isin import is_valid_isin


def _isin(first11: str) -> str:
    for d in "0123456789":
        if is_valid_isin(first11 + d):
            return first11 + d
    raise AssertionError(first11)


ACME_OLD = _isin("INE123A0101")
ACME_NEW = _isin("INE123A0102")
BETA = _isin("INE456B0101")
GAMMA = _isin("INE789C0101")

LEGACY_DAYS = [dt.date(2024, 7, d) for d in (1, 2, 3, 4, 5)]
UDIFF_DAYS = [dt.date(2024, 7, d) for d in (8, 9, 10, 11, 12, 15, 16)]
ALL_DAYS = LEGACY_DAYS + UDIFF_DAYS
SPLIT_EX = dt.date(2024, 7, 10)
BONUS_EX = dt.date(2024, 7, 11)
RENAME_ON = dt.date(2024, 7, 12)


def rows_for(day: dt.date) -> list[dict]:
    i = ALL_DAYS.index(day)
    acme = 500.0 + i
    if day >= SPLIT_EX:
        acme /= 5
    beta = 200.0 + i
    if day >= BONUS_EX:
        beta /= 2
    gamma = 50.0 + 0.5 * i
    prev = ALL_DAYS[i - 1] if i else None
    out = []
    for sym, isin, px, prev_px in [
        ("ACME", ACME_NEW if day >= SPLIT_EX else ACME_OLD, acme,
         None if prev is None else (500.0 + i - 1) / (5 if day >= SPLIT_EX else 1)),
        ("BETA", BETA, beta,
         None if prev is None else (200.0 + i - 1) / (2 if day >= BONUS_EX else 1)),
        ("GAMMA" if day >= RENAME_ON else "OLDG", GAMMA, gamma,
         None if prev is None else 50.0 + 0.5 * (i - 1)),
    ]:
        out.append({"symbol": sym, "isin": isin, "close": round(px, 2),
                    "prev_close": round(prev_px, 2) if prev_px else round(px, 2),
                    "volume": 10000 + 10 * i, "deliv": 4000 + 5 * i})
    return out


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def _csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


UDIFF_HEADER = ["TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN",
                "TckrSymb", "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp",
                "FinInstrmNm", "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
                "PrvsClsgPric", "UndrlygPric", "SttlmPric", "OpnIntrst", "ChngInOpnIntrst",
                "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty", "Rmks",
                "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4"]


def udiff(day: dt.date) -> bytes:
    rows = []
    for n, r in enumerate(rows_for(day)):
        for ssn, px in (("I1", r["close"] * 1.01), ("F1", r["close"])):
            rows.append([day.isoformat(), day.isoformat(), "CM", "NSE", "STK", 1000 + n,
                         r["isin"], r["symbol"], "EQ", "", "", "", "", f"{r['symbol']} LTD",
                         px, px, px, px, px, r["prev_close"], "", px, "", "",
                         r["volume"], r["volume"] * px, 100, ssn, 1, "", "", "", "", ""])
    name = f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv"
    return _zip(name, _csv(UDIFF_HEADER, rows))


def legacy(day: dt.date) -> bytes:
    header = ["SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "LAST", "PREVCLOSE",
              "TOTTRDQTY", "TOTTRDVAL", "TIMESTAMP", "TOTALTRADES", "ISIN", ""]
    ts = f"{day:%d}-{day:%b}-{day:%Y}".upper()
    rows = [[r["symbol"], "EQ", r["close"], r["close"], r["close"], r["close"], r["close"],
             r["prev_close"], r["volume"], r["volume"] * r["close"], ts, 100, r["isin"], ""]
            for r in rows_for(day)]
    return _zip(f"cm{day:%d}{day:%b}{day:%Y}bhav.csv".upper(), _csv(header, rows))


def sec_full(day: dt.date) -> bytes:
    header = ["SYMBOL", " SERIES", " DATE1", " PREV_CLOSE", " OPEN_PRICE", " HIGH_PRICE",
              " LOW_PRICE", " LAST_PRICE", " CLOSE_PRICE", " AVG_PRICE", " TTL_TRD_QNTY",
              " TURNOVER_LACS", " NO_OF_TRADES", " DELIV_QTY", " DELIV_PER"]
    rows = [[r["symbol"], " EQ", f" {day:%d-%b-%Y}", r["prev_close"], r["close"], r["close"],
             r["close"], r["close"], r["close"], r["close"], f" {r['volume']}", 1.0, 100,
             f" {r['deliv']}", f" {100 * r['deliv'] / r['volume']:.2f}"]
            for r in rows_for(day)]
    return _csv(header, rows).encode()


def mto(day: dt.date) -> bytes:
    lines = ["Security Wise Delivery Position - Compulsory Rolling Settlement",
             f"10,MTO,{day:%d%m%Y},123456,0000000",
             f"Trade Date <{day:%d-%b-%Y}>,Settlement Type <N>,Settlement No <2024123>",
             "Record Type,Sr No,Name of Security,Quantity Traded,"
             "Deliverable Quantity(gross across client level),"
             "% of Deliverable Quantity to Traded Quantity"]
    for n, r in enumerate(rows_for(day), start=1):
        lines.append(f"20,{n},{r['symbol']},EQ,{r['volume']},{r['deliv']},"
                     f"{100 * r['deliv'] / r['volume']:.2f}")
    return ("\n".join(lines) + "\n").encode()


def equity_list() -> bytes:
    header = ["SYMBOL", "NAME OF COMPANY", " SERIES", " DATE OF LISTING", " PAID UP VALUE",
              " MARKET LOT", " ISIN NUMBER", " FACE VALUE"]
    rows = [["ACME", "Acme Industries Limited", "EQ", "06-OCT-2008", 2, 1, ACME_NEW, 2],
            ["BETA", "Beta Chemicals Limited", "EQ", "15-MAR-2010", 10, 1, BETA, 10],
            ["GAMMA", "Gamma Foods Limited", "EQ", "01-JUN-2015", 10, 1, GAMMA, 10]]
    return _csv(header, rows).encode()


def index_close(day: dt.date) -> bytes:
    i = ALL_DAYS.index(day)
    header = ["Index Name", "Index Date", "Open Index Value", "High Index Value",
              "Low Index Value", "Closing Index Value", "Points Change", "Change(%)", "Volume",
              "Turnover (Rs. Cr.)", "P/E", "P/B", "Div Yield"]
    v = 22000 + 10 * i
    rows = [["Nifty 500", f"{day:%d-%m-%Y}", v, v, v, v, 10, 0.05, 1, 1, 22, 4, 1.2],
            ["Nifty 50", f"{day:%d-%m-%Y}", 24000, 24000, 24000, 24000, 1, 0.01, 1, 1, 22, 4, 1]]
    return _csv(header, rows).encode()


def holidays() -> bytes:
    return json.dumps({"CM": [
        {"tradingDate": "17-Jul-2024", "weekDay": "Wednesday", "description": "Muharram",
         "Sr_no": 1}],
        "FO": []}).encode()


def corporate_actions() -> bytes:
    return json.dumps([
        {"symbol": "ACME", "series": "EQ", "ind": "-", "faceVal": "10",
         "subject": "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
         "exDate": "10-Jul-2024", "recDate": "10-Jul-2024", "comp": "Acme Industries Limited",
         "isin": ACME_OLD, "caBroadcastDate": "20-Jun-2024 17:05:00"},
        {"symbol": "BETA", "series": "EQ", "ind": "-", "faceVal": "10", "subject": "Bonus 1:1",
         "exDate": "11-Jul-2024", "recDate": "11-Jul-2024", "comp": "Beta Chemicals Limited",
         "isin": BETA, "caBroadcastDate": "21-Jun-2024 18:00:00"},
        {"symbol": "BETA", "series": "EQ", "ind": "-", "faceVal": "10",
         "subject": "Interim Dividend - Rs 2 Per Share / Special Dividend - Rs 1.50 Per Share",
         "exDate": "05-Jul-2024", "recDate": "05-Jul-2024", "comp": "Beta Chemicals Limited",
         "isin": BETA, "caBroadcastDate": None},
    ]).encode()


def asm() -> bytes:
    return json.dumps({"longterm": {"data": [
        {"symbol": "GAMMA", "companyName": "Gamma Foods", "isin": GAMMA,
         "asmSurvIndicator": "Stage II"}]},
        "shortterm": {"data": []}}).encode()


def gsm(empty: bool = False) -> bytes:
    if empty:
        return json.dumps([]).encode()
    return json.dumps([{"symbol": "ZETA", "companyName": "Zeta Ltd", "isin": "INE000Z01010",
                        "gsmStage": "Stage 1"}]).encode()


def announcements() -> bytes:
    return json.dumps([
        {"symbol": "BETA", "desc": "Resignation of Chief Financial Officer (CFO)",
         "an_dt": "12-Jul-2024 19:02:11", "exchdisstime": "12-Jul-2024 19:02:13",
         "attchmntText": "Resignation of Mr X as Chief Financial Officer", "seq_id": "1001",
         "attchmntFile": "https://nsearchives.nseindia.com/corporate/BETA_1001.pdf"},
        {"symbol": "ACME", "desc": "Outcome of Board Meeting",
         "an_dt": "15-Jul-2024 16:10:00", "exchdisstime": "15-Jul-2024 16:10:01",
         "attchmntText": "Financial results for the quarter ended June 30, 2024",
         "seq_id": "1002", "attchmntFile": None},
    ]).encode()


def bse_scrips() -> bytes:
    return json.dumps([
        {"SCRIP_CD": "500111", "Scrip_Name": "Acme Industries Ltd", "Status": "Active",
         "GROUP": "B ", "FACE_VALUE": "2.00", "ISIN_NUMBER": ACME_NEW, "INDUSTRY": "Chemicals",
         "scrip_id": "ACME"},
        {"SCRIP_CD": "500222", "Scrip_Name": "Beta Chemicals Ltd", "Status": "Active",
         "GROUP": "A ", "FACE_VALUE": "10.00", "ISIN_NUMBER": BETA, "INDUSTRY": "Chemicals",
         "scrip_id": "BETA"},
    ]).encode()


def angel_master() -> bytes:
    return json.dumps([
        {"token": "11", "symbol": "ACME-EQ", "name": "ACME", "expiry": "", "strike": "-1",
         "lotsize": "1", "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5"},
        {"token": "22", "symbol": "BETA-EQ", "name": "BETA", "expiry": "", "strike": "-1",
         "lotsize": "1", "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5"},
        {"token": "99", "symbol": "NIFTY24JULFUT", "name": "NIFTY", "expiry": "25JUL2024",
         "strike": "-1", "lotsize": "25", "instrumenttype": "FUTIDX", "exch_seg": "NFO",
         "tick_size": "5"},
    ]).encode()


def quote(symbol: str) -> bytes:
    info = {"ACME": ("Industrials", "Capital Goods", "Industrial Products", "Castings & Forgings"),
            "BETA": ("Commodities", "Chemicals", "Chemicals & Petrochemicals",
                     "Specialty Chemicals"),
            "GAMMA": ("Fast Moving Consumer Goods", "FMCG", "Food Products", "Packaged Foods")}
    m = info[symbol]
    return json.dumps({"info": {"symbol": symbol}, "industryInfo": {
        "macro": m[0], "sector": m[1], "industry": m[2], "basicIndustry": m[3]}}).encode()
