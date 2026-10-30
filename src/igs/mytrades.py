"""Your own trades on Upstox, the bets you place by hand: followed as they happen and
measured after charges, so the app can show what has worked for you and what hasn't
(docs/MY_TRADES.md). Nothing here places, changes or cancels an order.

- **Import.** The day's trades, with their time, product and order, on each intraday check
  (every 5 minutes in the session). On request, Upstox's trade history of the last three
  financial years: dates only, without times, products or orders.
- **Yours only.** The app's own orders are left out: every fill in a stock on a day the
  app traded that stock.
- **Bets.** A bet runs from a flat position to flat again in one instrument: intraday,
  delivery or F&O, long or short. Charges are estimated from config/costs.yaml.
- **The market around each bet.** For a timed intraday bet in a stock: the stock's move
  before it, the Nifty, what the app had said about the stock, and how far the price went
  for and against it while it was open.
- **The record.** It groups the bets by those and says which groups stood out in the
  earlier bets, and whether that held in the later ones.
- **Telegram** (the trading chat): a note when a bet opens, with your record on similar
  bets, and when it closes, and a summary of the day.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from igs.timeutil import IST, utc_now

API = "https://api.upstox.com"
DAY_TRADES = "/v2/order/trades/get-trades-for-day"
HISTORY = "/v2/charges/historical-trades"
PAGE_SIZE = 5000
NIFTY = "NSE_INDEX|Nifty 50"
SEGMENTS = {"NSE_EQ": "EQ", "BSE_EQ": "EQ", "NSE_FO": "FO", "BSE_FO": "FO",
            "MCX_FO": "COM", "NCD_FO": "CD", "BCD_FO": "CD"}
TIME_FORMATS = ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S")
OPEN, CLOSE = dt.time(9, 15), dt.time(15, 30)
BAR = dt.timedelta(minutes=5)
MIN_BETS = 15              # in a group before it can stand out
MIN_LATER = 5              # later bets before saying whether it held
SIMILAR_MIN = 5            # similar bets before the open note gives their record
CONTEXTS_PER_RUN = 20      # older bets' market context filled in per check, after the close
KIND_LABEL = {"intraday": "intraday", "delivery": "delivery", "fno": "F&O", "other": "other"}
DEFAULTS = {"enabled": True, "notes": True}


def rule(cfg=None) -> dict:
    """rules.my_trades in config/alerts.yaml over DEFAULTS."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get("my_trades") or {})}


# --------------------------------------------------------------------------- Upstox


class AccountError(RuntimeError):
    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


class Account:
    """Read-only calls on the Upstox account."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self.client = httpx.Client(base_url=API, timeout=20, transport=transport, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json"})

    def close(self) -> None:
        self.client.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = self.client.get(path, params=params)
        except httpx.HTTPError:
            raise AccountError("Upstox couldn't be reached; the next check tries again") \
                from None
        code = None
        if response.status_code != 200:
            try:
                errors = response.json().get("errors") or []
                code = errors[0].get("errorCode") if errors else None
            except (ValueError, AttributeError, IndexError, TypeError):
                pass
        if response.status_code in (401, 403):
            raise AccountError(
                "Upstox won't give this server the account's trades from its IP: register "
                "it (static IP) for the token" if code == "UDAPI1154" else
                "Upstox refused the token for the account's trades; renew it in Settings, "
                "Upstox tab", auth=True)
        if response.status_code != 200:
            raise AccountError(f"Upstox returned HTTP {response.status_code}"
                               + (f" ({code})" if code else ""))
        try:
            body = response.json()
            if body["status"] != "success":
                raise ValueError
            return body
        except (ValueError, KeyError, TypeError):
            raise AccountError("Upstox returned an invalid answer") from None

    def day_trades(self) -> list[dict]:
        data = self._get(DAY_TRADES).get("data") or []
        if not isinstance(data, list):
            raise AccountError("Upstox returned an invalid list of trades")
        return data

    def history(self, start: dt.date, end: dt.date) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while True:
            body = self._get(HISTORY, {"start_date": f"{start}", "end_date": f"{end}",
                                       "page_number": page, "page_size": PAGE_SIZE})
            data = body.get("data") or []
            if not isinstance(data, list):
                raise AccountError("Upstox returned an invalid trade history")
            rows += data
            meta = (body.get("meta_data") or body.get("metadata") or {}).get("page") or {}
            if not data or page >= int(meta.get("total_pages") or 1):
                return rows
            page += 1


def tokens() -> list[str]:
    """The trading token, then the Analytics token: since June 2026 it reads the account's
    trades too, from a registered static IP."""
    from igs.intraday.scanner import token
    from igs.intraday.trading import trading_token
    return list(dict.fromkeys(t for t in (trading_token(), token()) if t))


def with_account(fn: Callable[[Account], Any], transport=None) -> Any:
    """fn(account) with the first token Upstox accepts."""
    found = tokens()
    if not found:
        raise AccountError("No Upstox token: save one in Settings, Upstox tab")
    refused = None
    for t in found:
        account = Account(t, transport)
        try:
            return fn(account)
        except AccountError as exc:
            if not exc.auth:
                raise
            refused = exc
        finally:
            account.close()
    raise refused


# --------------------------------------------------------------------------- fills


def _time(text: Any) -> dt.datetime | None:
    if not text:
        return None
    for fmt in TIME_FORMATS:
        try:
            return dt.datetime.strptime(str(text).strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    try:
        at = dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=IST)


def _option(symbol: str) -> str | None:
    s = symbol.replace(" ", "").upper()
    return s[-2:] if s.endswith(("CE", "PE")) else None


def day_fill(row: dict) -> dict | None:
    """A fill from the day's trades; None for a segment not followed."""
    key = str(row.get("instrument_token") or "")
    segment = SEGMENTS.get(key.split("|")[0])
    if segment is None:
        return None
    at = _time(row.get("exchange_timestamp")) or _time(row.get("order_timestamp"))
    if at is None:
        raise ValueError("a trade without a time")
    symbol = str(row.get("trading_symbol") or row.get("tradingsymbol") or key)
    if segment == "EQ":
        symbol = re.sub(r"-(EQ|BE|BZ|SM|ST)$", "", symbol)     # the series Upstox adds
    return {"exchange": str(row["exchange"]), "segment": segment,
            "trade_date": at.astimezone(IST).date(), "trade_id": str(row["trade_id"]),
            "traded_at": at, "instrument_key": key, "symbol": symbol,
            "isin": key.split("|", 1)[1] if segment == "EQ" else None, "expiry": None,
            "strike": None, "option_type": _option(symbol) if segment != "EQ" else None,
            "product": row.get("product"), "side": str(row["transaction_type"]).lower(),
            "quantity": int(row["quantity"]), "price": Decimal(str(row["average_price"])),
            "order_id": row.get("order_id"), "order_type": row.get("order_type"),
            "source": "day"}


def history_fill(row: dict) -> dict | None:
    """A fill from the trade history; None for mutual funds and rows without a trade id."""
    segment = row.get("segment")
    if segment not in ("EQ", "FO", "COM", "CD") or not row.get("trade_id"):
        return None
    key = row.get("instrument_token") or None
    try:
        expiry = dt.date.fromisoformat(str(row["expiry"])[:10]) if row.get("expiry") else None
    except ValueError:
        expiry = None
    strike = float(row.get("strike_price") or 0) or None
    return {"exchange": str(row["exchange"]), "segment": segment,
            "trade_date": dt.date.fromisoformat(str(row["trade_date"])[:10]),
            "trade_id": str(row["trade_id"]), "traded_at": None, "instrument_key": key,
            "symbol": str(row.get("symbol") or row.get("scrip_name") or ""),
            "isin": row.get("isin") or (key.split("|", 1)[1] if key and segment == "EQ"
                                        else None),
            "expiry": expiry, "strike": strike,
            "option_type": row.get("option_type") if row.get("option_type") in ("CE", "PE")
            else None, "product": None, "side": str(row["transaction_type"]).lower(),
            "quantity": int(row["quantity"]), "price": Decimal(str(row["price"])),
            "order_id": None, "order_type": None, "source": "history"}


COLUMNS = ("exchange", "segment", "trade_date", "trade_id", "traded_at", "instrument_key",
           "symbol", "isin", "expiry", "strike", "option_type", "product", "side", "quantity",
           "price", "order_id", "order_type", "source")


def _store(conn, fills: Iterable[dict]) -> int:
    added = 0
    for f in fills:
        cur = conn.execute(
            f"""insert into upstox_fill ({', '.join(COLUMNS)})
                values ({', '.join(['%s'] * len(COLUMNS))})
                on conflict (exchange, trade_date, trade_id) do nothing""",
            [f[c] for c in COLUMNS])
        added += cur.rowcount
    return added


def _log(conn, kind: str, start: dt.date, end: dt.date, seen: int, added: int,
         error: str | None = None) -> None:
    conn.execute("""insert into my_trades_import (kind, start_date, end_date, fills_seen,
                        fills_added, error) values (%s, %s, %s, %s, %s, %s)""",
                 (kind, start, end, seen, added, error))


def import_day(conn, account: Account, now: dt.datetime | None = None) -> tuple[int, int]:
    """The day's trades: (fills seen, fills new)."""
    today = (now or utc_now()).astimezone(IST).date()
    fills, bad = [], 0
    for row in account.day_trades():
        try:
            f = day_fill(row)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            bad += 1
            continue
        if f is not None:
            fills.append(f)
    added = _store(conn, fills)
    _log(conn, "day", today, today, len(fills), added,
         f"{bad} trades Upstox gave without a time or a price" if bad else None)
    conn.commit()
    return len(fills), added


def history_start(today: dt.date) -> dt.date:
    """The first day of the trade history Upstox gives: three financial years back."""
    fy = today.year if today.month >= 4 else today.year - 1
    return dt.date(fy - 2, 4, 1)


def _months(start: dt.date, end: dt.date) -> Iterable[tuple[dt.date, dt.date]]:
    while start <= end:
        nxt = (start.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        yield start, min(end, nxt - dt.timedelta(days=1))
        start = nxt


def import_history(conn, account: Account, start: dt.date | None = None,
                   end: dt.date | None = None, today: dt.date | None = None
                   ) -> tuple[int, int]:
    """Upstox's trade history, a month at a time: (fills seen, fills new). Days whose own
    trades were imported on the day keep those (they have times); the history adds only
    the days without."""
    today = today or utc_now().astimezone(IST).date()
    start = max(start or history_start(today), history_start(today))
    end = min(end or today, today)
    covered = {r[0] for r in conn.execute(
        "select distinct trade_date from upstox_fill where source = 'day'").fetchall()}
    seen = added = 0
    for a, b in _months(start, end):
        fills = [f for f in (history_fill(r) for r in account.history(a, b))
                 if f is not None and f["trade_date"] not in covered]
        seen += len(fills)
        added += _store(conn, fills)
    _log(conn, "history", start, end, seen, added)
    conn.commit()
    return seen, added


def last_import(conn, kind: str) -> dict | None:
    cur = conn.execute("""select started_at, start_date, end_date, fills_seen, fills_added,
                                 error from my_trades_import where kind = %s
                          order by started_at desc limit 1""", (kind,))
    row = cur.fetchone()
    return dict(zip([d.name for d in cur.description], row, strict=True)) if row else None


# --------------------------------------------------------------------------- bets


@dataclass
class Bet:
    key: str
    kind: str                         # intraday, delivery, fno, other
    segment: str
    symbol: str
    instrument_key: str | None
    isin: str | None
    side: str | None                  # long, short; None: a day's round trip without times
    opened_on: dt.date
    opened_at: dt.datetime | None
    source: str
    option: bool = False
    company_id: int | None = None
    closed_on: dt.date | None = None
    closed_at: dt.datetime | None = None
    quantity: int = 0                 # the largest position
    buy_qty: int = 0
    sell_qty: int = 0
    buy_value: Decimal = Decimal(0)
    sell_value: Decimal = Decimal(0)
    orders: dict = field(default_factory=dict)   # order -> [side, value, trade_date]
    charges: Decimal | None = None
    context: dict = field(default_factory=dict)

    def add(self, f: dict, quantity: int) -> None:
        value = quantity * f["price"]
        if f["side"] == "buy":
            self.buy_qty += quantity
            self.buy_value += value
        else:
            self.sell_qty += quantity
            self.sell_value += value
        order = f["order_id"] or f"{f['trade_date']}|{f['side']}"
        self.orders.setdefault(order, [f["side"], Decimal(0), f["trade_date"]])[1] += value

    @property
    def closed(self) -> bool:
        return self.closed_on is not None

    @property
    def entry(self) -> Decimal | None:
        qty, value = ((self.sell_qty, self.sell_value) if self.side == "short"
                      else (self.buy_qty, self.buy_value))
        return value / qty if qty else None

    @property
    def exit(self) -> Decimal | None:
        qty, value = ((self.buy_qty, self.buy_value) if self.side == "short"
                      else (self.sell_qty, self.sell_value))
        return value / qty if qty else None

    @property
    def stake(self) -> Decimal:
        """What went in: the opening side's value."""
        return self.sell_value if self.side == "short" else self.buy_value

    @property
    def gross(self) -> Decimal | None:
        return self.sell_value - self.buy_value if self.closed else None

    @property
    def net(self) -> Decimal | None:
        if not self.closed or self.charges is None:
            return None
        return self.gross - self.charges

    @property
    def net_pct(self) -> float | None:
        return float(self.net / self.stake * 100) if self.net is not None and self.stake \
            else None

    @property
    def minutes(self) -> float | None:
        if self.opened_at and self.closed_at:
            return (self.closed_at - self.opened_at).total_seconds() / 60
        return None

    @property
    def days(self) -> int | None:
        return (self.closed_on - self.opened_on).days if self.closed_on else None

    @property
    def direction(self) -> int:
        return -1 if self.side == "short" else 1


def _fill_key(f: dict) -> str:
    return f"{f['exchange']}|{f['trade_date']}|{f['trade_id']}"


def _walk(fills: list[dict], kind: str) -> tuple[list[Bet], int]:
    """Bets from one instrument's fills in order, flat to flat; a fill that reverses the
    position closes one bet and opens the next. Delivery can't open short: a sale beyond
    the shares bought in the record sold shares bought before it (counted, left out)."""
    bets: list[Bet] = []
    pos, bet, unmatched = 0, None, 0
    for f in fills:
        remaining = f["quantity"] if f["side"] == "buy" else -f["quantity"]
        opened_here = 0
        while remaining:
            if pos == 0:
                if kind == "delivery" and remaining < 0:
                    unmatched += 1
                    break
                opened_here += 1
                bet = Bet(key=_fill_key(f) + (f":{opened_here}" if opened_here > 1 else ""),
                          kind=kind, segment=f["segment"], symbol=f["symbol"],
                          instrument_key=f["instrument_key"], isin=f["isin"],
                          side="long" if remaining > 0 else "short",
                          opened_on=f["trade_date"], opened_at=f["traded_at"],
                          source=f["source"], option=f["option_type"] is not None)
                bets.append(bet)
            if pos == 0 or (pos > 0) == (remaining > 0):
                take = remaining
            else:
                take = max(remaining, -pos) if pos > 0 else min(remaining, -pos)
            bet.add(f, abs(take))
            pos += take
            remaining -= take
            bet.quantity = max(bet.quantity, abs(pos))
            if pos == 0:
                bet.closed_on, bet.closed_at = f["trade_date"], f["traded_at"]
    return bets, unmatched


def _identity(f: dict) -> tuple:
    if f["segment"] == "EQ":
        return ("EQ", f["isin"] or f["symbol"])
    return (f["segment"], f["instrument_key"] or (f["symbol"], f["expiry"], f["strike"],
                                                  f["option_type"]))


def build(fills: list[dict]) -> tuple[list[Bet], int]:
    """Bets from fills in time order (the app's own left out first): (bets, delivery sales
    of shares bought before the record)."""
    ledgers: dict[tuple, list[dict]] = defaultdict(list)
    untimed: dict[tuple, list[dict]] = defaultdict(list)
    for f in fills:
        ident = _identity(f)
        if f["segment"] != "EQ":
            ledgers[("fno" if f["segment"] == "FO" else "other", ident)].append(f)
        elif f["product"] in ("I", "CO"):
            ledgers[("intraday", ident, f["trade_date"])].append(f)
        elif f["product"] is None:
            untimed[(ident, f["trade_date"])].append(f)
        else:
            ledgers[("delivery", ident)].append(f)
    # A history day's fills in a stock: an intraday round trip when its buys and sales
    # match; otherwise delivery.
    for (ident, day), fs in untimed.items():
        bought = sum(f["quantity"] for f in fs if f["side"] == "buy")
        sold = sum(f["quantity"] for f in fs if f["side"] == "sell")
        if bought and bought == sold:
            ledgers[("intraday day", ident, day)] = fs
        else:
            ledgers[("delivery", ident)].extend(fs)
    bets, unmatched = [], 0
    for key, fs in ledgers.items():
        kind = key[0]
        if kind == "intraday day":
            bet = Bet(key=f"day|{_fill_key(fs[0])}", kind="intraday", segment="EQ",
                      symbol=fs[0]["symbol"], instrument_key=fs[0]["instrument_key"],
                      isin=fs[0]["isin"], side=None, opened_on=fs[0]["trade_date"],
                      opened_at=None, source="history", closed_on=fs[0]["trade_date"])
            for f in fs:
                bet.add(f, f["quantity"])
            bet.quantity = bet.buy_qty
            bets.append(bet)
            continue
        if kind == "delivery":
            fs = sorted(fs, key=lambda f: (f["trade_date"], f["traded_at"] or
                                           dt.datetime.min.replace(tzinfo=IST)))
        got, lost = _walk(fs, kind)
        bets += got
        unmatched += lost
    bets.sort(key=lambda b: (b.opened_on, b.opened_at or dt.datetime.min.replace(tzinfo=IST),
                             b.key))
    return bets, unmatched


def charges(bet: Bet, costs) -> Decimal | None:
    """Estimated charges on the bet's orders (config/costs.yaml): brokerage per order,
    STT, exchange and SEBI fees, stamp duty and GST, and for delivery the depository
    charge on each day shares were sold. None for commodities and currencies."""
    def pct(x) -> Decimal:
        return Decimal(str(x)) / 100

    buys = sum((v for s, v, _ in bet.orders.values() if s == "buy"), Decimal(0))
    sells = sum((v for s, v, _ in bet.orders.values() if s == "sell"), Decimal(0))
    if bet.kind == "intraday":
        r = costs.intraday
        brokerage = sum((min(v * pct(r.brokerage_pct), Decimal(str(r.brokerage_max_inr)))
                         for _, v, _ in bet.orders.values()), Decimal(0))
        fees = (buys + sells) * (pct(r.exchange_txn_pct) + pct(r.sebi_fee_pct))
        tax = sells * pct(r.stt_sell_pct) + buys * pct(r.stamp_duty_buy_pct)
        gst = pct(r.gst_pct)
    elif bet.kind == "delivery":
        st, b = costs.statutory, costs.brokerage
        brokerage = sum((min(v * pct(b.pct), Decimal(str(b.max_inr_per_order)))
                         for _, v, _ in bet.orders.values()), Decimal(0))
        fees = (buys + sells) * (pct(st.exchange_txn_pct) + pct(st.sebi_fee_pct))
        sell_days = {d for s, _, d in bet.orders.values() if s == "sell"}
        tax = (buys * pct(st.stt_buy_pct) + sells * pct(st.stt_sell_pct)
               + buys * pct(st.stamp_duty_buy_pct)
               + len(sell_days) * Decimal(str(st.dp_charge_inr_per_sell)))
        gst = pct(st.gst_pct)
    elif bet.kind == "fno":
        f = costs.fno
        leg = f.options if bet.option else f.futures
        brokerage = len(bet.orders) * Decimal(str(f.brokerage_inr_per_order))
        fees = (buys + sells) * (pct(leg.exchange_txn_pct) + pct(f.sebi_fee_pct))
        tax = sells * pct(leg.stt_sell_pct) + buys * pct(leg.stamp_duty_buy_pct)
        gst = pct(f.gst_pct)
    else:
        return None
    return (brokerage + fees + (brokerage + fees) * gst + tax).quantize(Decimal("0.01"))


def app_days(conn) -> set[tuple[str, dt.date]]:
    """(instrument, day) the app itself traded: its fills there aren't yours."""
    return {(r[0], r[1]) for r in conn.execute(
        """select instrument_key, trading_day from intraday_trade
           where status not in ('rejected', 'expired')""").fetchall()}


def fills(conn) -> list[dict]:
    cur = conn.execute(f"""select {', '.join(COLUMNS)} from upstox_fill
                           order by trade_date, traded_at nulls first, fill_id""")
    return [dict(zip(COLUMNS, r, strict=True)) for r in cur.fetchall()]


def bets(conn, costs=None) -> tuple[list[Bet], dict]:
    """Your bets, with charges, companies and stored market context: (bets, notes on
    what was left out)."""
    from igs.config import load_costs
    costs = costs or load_costs()
    every = fills(conn)
    theirs = app_days(conn)
    mine = [f for f in every if (f["instrument_key"], f["trade_date"]) not in theirs]
    out, unmatched = build(mine)
    isins = {b.isin for b in out if b.isin}
    companies = dict(conn.execute(
        """select distinct on (si.id_value) si.id_value, s.company_id
           from security_identifier si join security s using (security_id)
           where si.id_type = 'ISIN' and si.id_value = any(%s)
           order by si.id_value, si.valid_from desc""", (list(isins),)).fetchall()) \
        if isins else {}
    stored = dict(conn.execute("select bet_key, context from my_bet_context").fetchall())
    for b in out:
        b.charges = charges(b, costs)
        b.company_id = companies.get(b.isin)
        b.context = stored.get(b.key) or {}
    return out, {"app_fills": len(every) - len(mine), "unmatched_sales": unmatched}


# --------------------------------------------------------------------------- context


def _floor(at: dt.datetime) -> dt.datetime:
    at = at.astimezone(IST)
    return at.replace(minute=at.minute - at.minute % 5, second=0, microsecond=0)


def _prev_close(conn, isin: str | None, day: dt.date) -> float | None:
    if not isin:
        return None
    row = conn.execute("""select close::float8 from price_eod where isin = %s
                          and trade_date < %s order by trade_date desc limit 1""",
                       (isin, day)).fetchone()
    return row[0] if row else None


def signals(conn, bet: Bet) -> dict:
    """What the app had said about the stock that day."""
    out: dict[str, Any] = {}
    day, cid = bet.opened_on, bet.company_id
    if cid is not None:
        row = conn.execute("""select side from ml_intraday_pick
                              where session_date = %s and company_id = %s""",
                           (day, cid)).fetchone()
        out["ml_side"] = row[0] if row else None
        row = conn.execute("""select side from evening_pick
                              where trade_date = %s and company_id = %s""",
                           (day, cid)).fetchone()
        out["evening_side"] = row[0] if row else None
        row = conn.execute("""select r.rank from score_result r join score_run s using (run_id)
                              where r.company_id = %s and s.as_of < %s
                              order by s.as_of desc limit 1""",
                           (cid, dt.datetime.combine(day, dt.time(), tzinfo=IST))).fetchone()
        out["momentum_rank"] = row[0] if row else None
    if bet.isin:
        row = conn.execute("""select alert_side from intraday_spike
                              where trade_date = %s and isin = %s and alert_side is not null""",
                           (day, bet.isin)).fetchone()
        out["spike"] = None if row is None else ("rise" if row[0] == -1 else "fall")
    return out


def context(conn, bet: Bet, bars: list, nifty: list) -> dict:
    """The market around a timed bet, from its day's closed 5-minute candles (`bars`, and
    the Nifty's): before it, then (closed) while it was open and after it."""
    at, price, d = bet.opened_at, float(bet.entry), bet.direction
    out: dict[str, Any] = {"minutes_in": round(
        (at.astimezone(IST) - at.astimezone(IST).replace(hour=9, minute=15, second=0,
                                                         microsecond=0)).total_seconds() / 60)}
    prev = _prev_close(conn, bet.isin, bet.opened_on)
    if prev:
        out["day_move_pct"] = round((price / prev - 1) * 100, 3)
    before = [b for b in bars if b.start + BAR <= at]
    if bars:
        out["open_move_pct"] = round((price / bars[0].open - 1) * 100, 3)
    earlier = [b for b in before if b.start + BAR <= at - dt.timedelta(minutes=30)]
    if earlier:
        out["move_30m_pct"] = round((price / earlier[-1].close - 1) * 100, 3)
    volume = sum(b.volume for b in before)
    if volume > 0:
        vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in before) / volume
        out["vwap_gap_pct"] = round((price / vwap - 1) * 100, 3)
    if before and max(b.high for b in before) > min(b.low for b in before):
        hi, lo = max(b.high for b in before), min(b.low for b in before)
        out["range_pos"] = round((price - lo) / (hi - lo), 3)
    n_before = [b for b in nifty if b.start + BAR <= at]
    if n_before:
        out["nifty_move_pct"] = round((n_before[-1].close / nifty[0].open - 1) * 100, 3)
    if bet.closed_at is not None:
        start, end = _floor(at) + BAR, _floor(bet.closed_at)
        during = [b for b in bars if start <= b.start <= end]
        if during:
            best = max(b.high for b in during) if d > 0 else min(b.low for b in during)
            worst = min(b.low for b in during) if d > 0 else max(b.high for b in during)
            out["best_pct"] = round(d * (best / price - 1) * 100, 3)
            out["worst_pct"] = round(d * (worst / price - 1) * 100, 3)
        last = next((b for b in bars if b.start.astimezone(IST).time() == dt.time(15, 15)),
                    None)
        if last is not None:
            out["to_1515_pct"] = round(d * (last.open / price - 1) * 100, 3)
        after = [b for b in bars if b.start >= end + dt.timedelta(minutes=30)]
        if after:
            out["after_exit_30m_pct"] = round(
                d * (after[0].open / float(bet.exit) - 1) * 100, 3)
    out.update(signals(conn, bet))
    return out


def day_bars(feed, key: str, day: dt.date, now: dt.datetime) -> list:
    """The day's closed 5-minute candles in the session."""
    from igs.intraday import engine
    if day == now.astimezone(IST).date():
        return engine.session(feed.candles(key), now)
    return [b for b in feed.candles_between(key, day, day)
            if b.start.astimezone(IST).date() == day
            and OPEN <= b.start.astimezone(IST).time() < CLOSE]


def needs_context(b: Bet, now: dt.datetime) -> bool:
    """A closed, timed intraday bet in a stock, whose day is over, without its context."""
    return (b.kind == "intraday" and b.closed and b.opened_at is not None
            and b.instrument_key and not b.context
            and (b.opened_on < now.astimezone(IST).date()
                 or now.astimezone(IST).time() >= CLOSE))


def fill_contexts(conn, out: list[Bet], feed, now: dt.datetime,
                  limit: int = CONTEXTS_PER_RUN) -> int:
    """Store the market context of up to `limit` bets that need it, newest first."""
    done, nifty = 0, {}
    for b in sorted((b for b in out if needs_context(b, now)),
                    key=lambda b: b.opened_at, reverse=True)[:limit]:
        if b.opened_on not in nifty:
            nifty[b.opened_on] = day_bars(feed, NIFTY, b.opened_on, now)
        b.context = context(conn, b, day_bars(feed, b.instrument_key, b.opened_on, now),
                            nifty[b.opened_on])
        conn.execute("""insert into my_bet_context (bet_key, context) values (%s, %s)
                        on conflict (bet_key) do nothing""", (b.key, Jsonb(b.context)))
        done += 1
    conn.commit()
    return done


def fill_signals(conn, out: list[Bet]) -> int:
    """Store what the app had said about the stock for bets that get no candle context
    (delivery, and intraday days from the history): the momentum rank above all."""
    done = 0
    for b in out:
        if b.context or b.segment != "EQ" or b.company_id is None or (
                b.kind == "intraday" and b.opened_at is not None):
            continue
        b.context = signals(conn, b)
        conn.execute("""insert into my_bet_context (bet_key, context) values (%s, %s)
                        on conflict (bet_key) do nothing""", (b.key, Jsonb(b.context)))
        done += 1
    conn.commit()
    return done


# --------------------------------------------------------------------------- the record


def _t(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    return statistics.fmean(values) / (sd / math.sqrt(len(values))) if sd else None


def summary(closed: list[Bet]) -> dict:
    nets = [float(b.net) for b in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_profit = sum(float(b.gross) for b in closed if b.gross > 0)
    charge = sum(float(b.charges) for b in closed)
    return {"bets": len(nets), "won_pct": 100 * len(wins) / len(nets) if nets else None,
            "net_inr": sum(nets), "gross_inr": sum(float(b.gross) for b in closed),
            "charges_inr": charge,
            "charges_share_pct": 100 * charge / gross_profit if gross_profit else None,
            "avg_win_inr": statistics.fmean(wins) if wins else None,
            "avg_loss_inr": statistics.fmean(losses) if losses else None,
            "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
            "avg_net_pct": statistics.fmean(p) if (p := [b.net_pct for b in closed
                                                         if b.net_pct is not None]) else None,
            "t": _t(nets)}


def _with(move: float | None, d: int, big: float) -> str | None:
    if move is None:
        return None
    if abs(move) < big:
        return f"under {big:g}%"
    return f"with a {big:g}%+ move" if move * d > 0 else f"against a {big:g}%+ move"


def _held(b: Bet) -> str | None:
    if b.kind == "intraday":
        m = b.minutes
        return None if m is None else ("under 15 minutes" if m < 15 else "15–60 minutes"
                                       if m < 60 else "1–3 hours" if m < 180 else "3 hours+")
    d = b.days
    return None if d is None else ("under a week" if d < 7 else "1–4 weeks" if d < 28
                                   else "1–6 months" if d < 183 else "6 months+")


def _entry_time(b: Bet) -> str | None:
    if b.opened_at is None or b.kind != "intraday":
        return None
    t = b.opened_at.astimezone(IST).time()
    return ("09:15–09:45" if t < dt.time(9, 45) else "09:45–11:00" if t < dt.time(11)
            else "11:00–13:00" if t < dt.time(13) else "13:00–15:30")


def _size(b: Bet) -> str:
    v = float(b.stake)
    return ("under ₹50,000" if v < 5e4 else "₹50,000–2 lakh" if v < 2e5
            else "₹2–5 lakh" if v < 5e5 else "₹5 lakh+")


def _app(b: Bet) -> str | None:
    c = b.context
    if b.kind != "intraday" or not c or b.side is None:
        return None
    side = c.get("ml_side") or c.get("evening_side")
    if side is None:
        return "no app call that day"
    return "with the app's call" if side == b.direction else "against the app's call"


def _rank(b: Bet) -> str | None:
    if b.kind != "delivery" or b.company_id is None:
        return None
    r = (b.context or {}).get("momentum_rank")
    return ("unranked" if r is None else "top 100" if r <= 100 else "101–500" if r <= 500
            else "below 500")


def _sequence(closed: list[Bet]) -> dict[str, tuple[str, str]]:
    """Each timed intraday bet's place in its day, and what the bet before it did."""
    out: dict[str, tuple[str, str]] = {}
    by_day: dict[dt.date, list[Bet]] = defaultdict(list)
    for b in closed:
        if b.kind == "intraday" and b.opened_at is not None:
            by_day[b.opened_on].append(b)
    for day_bets in by_day.values():
        day_bets.sort(key=lambda b: b.opened_at)
        for i, b in enumerate(day_bets):
            prior = [p for p in day_bets[:i] if p.closed_at and p.closed_at <= b.opened_at]
            after = ("first of the day" if not prior else
                     "after a loss" if prior[-1].net is not None and prior[-1].net <= 0
                     else "after a win")
            out[b.key] = (("1st", "2nd", "3rd")[i] if i < 3 else "4th+", after)
    return out


def groupings(closed: list[Bet]) -> list[tuple[str, Callable[[Bet], str | None]]]:
    seq = _sequence(closed)
    return [
        ("Kind", lambda b: KIND_LABEL[b.kind]),
        ("Side", lambda b: {"long": "long", "short": "short"}.get(b.side)),
        ("Entry time", _entry_time),
        ("The day's move before it", lambda b: _with(b.context.get("day_move_pct"),
                                                     b.direction, 2)),
        ("The 30 minutes before it", lambda b: _with(b.context.get("move_30m_pct"),
                                                     b.direction, 0.5)),
        ("Held", _held),
        ("Bet of the day", lambda b: seq.get(b.key, (None, None))[0]),
        ("After", lambda b: seq.get(b.key, (None, None))[1]),
        ("Size", _size),
        ("The app", _app),
        ("Momentum rank", _rank),
    ]


def _cell(group: str, value: str, bs: list[Bet]) -> dict:
    nets = [float(b.net) for b in bs]
    pcts = [b.net_pct for b in bs if b.net_pct is not None]
    return {"group": group, "value": value, "bets": len(bs),
            "won_pct": 100 * sum(x > 0 for x in nets) / len(nets),
            "net_inr": sum(nets), "avg_net_inr": statistics.fmean(nets),
            "avg_net_pct": statistics.fmean(pcts) if pcts else None, "t": _t(nets)}


def breakdown(closed: list[Bet]) -> list[dict]:
    rows = []
    for name, fn in groupings(closed):
        cells: dict[str, list[Bet]] = defaultdict(list)
        for b in closed:
            v = fn(b)
            if v is not None:
                cells[v].append(b)
        rows += [_cell(name, v, bs) for v, bs in sorted(cells.items())]
    return rows


def patterns(closed: list[Bet]) -> list[dict]:
    """Groups that stood out in the earlier two thirds of the bets (at least MIN_BETS, t
    of 2 or more either way), and what they did in the later third, which didn't choose
    them."""
    ordered = sorted(closed, key=lambda b: (b.closed_on, b.closed_at or
                                            dt.datetime.min.replace(tzinfo=IST)))
    cut = len(ordered) * 2 // 3
    earlier, later = ordered[:cut], ordered[cut:]
    out = []
    for name, fn in groupings(ordered):
        cells: dict[str, list[Bet]] = defaultdict(list)
        for b in earlier:
            v = fn(b)
            if v is not None:
                cells[v].append(b)
        for value, bs in sorted(cells.items()):
            t = _t([float(b.net) for b in bs])
            if len(bs) < MIN_BETS or t is None or abs(t) < 2:
                continue
            first = _cell(name, value, bs)
            then = [b for b in later if fn(b) == value]
            mean = statistics.fmean(float(b.net) for b in then) if then else None
            held = ("too few later bets" if len(then) < MIN_LATER
                    else "held" if (mean > 0) == (first["avg_net_inr"] > 0)
                    else "didn't hold")
            out.append({**first, "later_bets": len(then), "later_avg_net_inr": mean,
                        "held": held})
    return out


def exits(closed: list[Bet]) -> dict | None:
    """Timed intraday bets: how your exits compare with the best and worst prices while
    the bet was open, holding to 15:15, and the 30 minutes after you closed."""
    timed = [b for b in closed if b.kind == "intraday" and b.context.get("best_pct")
             is not None]
    if not timed:
        return None

    def mean(key, bs=timed):
        xs = [b.context[key] for b in bs if b.context.get(key) is not None]
        return (statistics.fmean(xs), len(xs)) if xs else (None, 0)

    gross = [float(b.gross / b.stake * 100) for b in timed]
    winners = [b for b in timed if b.gross > 0]
    losers = [b for b in timed if b.gross <= 0]
    return {"bets": len(timed), "your_exit_pct": statistics.fmean(gross),
            "best_pct": mean("best_pct")[0], "worst_pct": mean("worst_pct")[0],
            "to_1515_pct": mean("to_1515_pct")[0],
            "after_exit_30m_pct": mean("after_exit_30m_pct")[0],
            "winners": len(winners), "winners_best_pct": mean("best_pct", winners)[0],
            "winners_exit_pct": statistics.fmean(float(b.gross / b.stake * 100)
                                                 for b in winners) if winners else None,
            "losers": len(losers), "losers_worst_pct": mean("worst_pct", losers)[0]}


def record(conn, costs=None) -> dict:
    out, left = bets(conn, costs)
    closed = [b for b in out if b.closed and b.net is not None]
    return {"bets": out, "closed": closed, "open": [b for b in out if not b.closed],
            "summary": summary(closed),
            "by_kind": {k: summary([b for b in closed if b.kind == k])
                        for k in KIND_LABEL if any(b.kind == k for b in closed)},
            "breakdown": breakdown(closed), "patterns": patterns(closed),
            "exits": exits(closed), "left_out": left,
            "since": min((b.opened_on for b in out), default=None)}


# --------------------------------------------------------------------------- notes


def _inr(x: float | Decimal | None, sign: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"₹{float(x):+,.0f}" if sign else f"₹{float(x):,.0f}"


def _value(x: Decimal) -> str:
    v = float(x)
    return f"₹{v / 1e5:,.2f} lakh" if v >= 1e5 else f"₹{v:,.0f}"


def similar(closed: list[Bet], bet: Bet, ctx: dict) -> tuple[str, list[Bet]]:
    """Your earlier closed bets like this one: the same kind and side, and the same
    relation to the day's move (2% or more with it, against it, or less)."""
    move = _with(ctx.get("day_move_pct"), bet.direction, 2)
    name = f"{KIND_LABEL[bet.kind]} {'buys' if bet.side == 'long' else 'short sales'}"
    if move and not move.startswith("under"):
        rose = (bet.direction > 0) == move.startswith("with")
        name += f" after a 2%+ {'rise' if rose else 'fall'} on the day"
    like = [b for b in closed if b.kind == bet.kind and b.side == bet.side
            and (move is None or _with(b.context.get("day_move_pct"), b.direction, 2) == move)]
    return name, like


def open_text(bet: Bet, ctx: dict, closed: list[Bet]) -> str:
    at = bet.opened_at.astimezone(IST) if bet.opened_at else None
    lines = [f"📒 You opened: {'BUY' if bet.side == 'long' else 'SHORT'} {bet.quantity} "
             f"{bet.symbol} ({KIND_LABEL[bet.kind]}) at ₹{float(bet.entry):,.2f}"
             + (f", {at:%H:%M}" if at else "") + f", {_value(bet.stake)}."]
    seen = []
    for key, label in (("day_move_pct", "on the day"), ("move_30m_pct", "in the last 30 minutes"),
                       ("vwap_gap_pct", "from the VWAP")):
        if ctx.get(key) is not None:
            seen.append(f"{ctx[key]:+.1f}% {label}")
    if ctx.get("nifty_move_pct") is not None:
        seen.append(f"Nifty {ctx['nifty_move_pct']:+.1f}% since the open")
    if seen:
        lines.append("Before it: " + ", ".join(seen) + ".")
    said = []
    for key, label in (("ml_side", "09:45 ML call"), ("evening_side", "evening pick")):
        if ctx.get(key) is not None:
            said.append(f"{label} {'buy' if ctx[key] == 1 else 'short'}")
    if ctx.get("spike"):
        said.append(f"spike alert ({ctx['spike']})")
    if said:
        lines.append("The app today: " + ", ".join(said) + ".")
    name, like = similar(closed, bet, ctx)
    if len(like) >= SIMILAR_MIN:
        s = summary(like)
        lines.append(f"Your record on {name}: {s['bets']} bets, {s['won_pct']:.0f}% won, "
                     f"{_inr(s['net_inr'])} net after charges.")
    else:
        lines.append(f"Your record on {name}: {len(like)} so far, too few to say.")
    return "\n".join(lines)


def close_text(bet: Bet, today: list[Bet]) -> str:
    held = (f"{bet.minutes:.0f} minutes" if bet.minutes is not None
            else f"{bet.days} days" if bet.days else "the same day")
    done = [b for b in today if b.closed and b.net is not None]
    pct = f" ({bet.net_pct:+.2f}%)" if bet.net_pct is not None else ""
    return (f"📒 Closed: {bet.symbol} {bet.side or ''} ({KIND_LABEL[bet.kind]}), {held}: "
            f"{_inr(bet.gross)} before charges, {_inr(bet.charges, sign=False)} charges, "
            f"{_inr(bet.net)} net{pct}.\nToday so far: {len(done)} bets, "
            f"{_inr(sum(float(b.net) for b in done))} net.")


def day_text(day: dt.date, today: list[Bet], closed: list[Bet]) -> str:
    done = [b for b in today if b.closed and b.net is not None]
    s, total = summary(done), summary(closed)
    lines = [f"📒 Your trades on {day:%d %b}: {s['bets']} bets, "
             f"{sum(float(b.net) > 0 for b in done)} won, {_inr(s['net_inr'])} net after "
             f"{_inr(s['charges_inr'], sign=False)} charges."]
    if done:
        best = max(done, key=lambda b: b.net)
        worst = min(done, key=lambda b: b.net)
        lines.append(f"Best: {best.symbol} {_inr(best.net)}; worst: {worst.symbol} "
                     f"{_inr(worst.net)}.")
    still = [b for b in today if not b.closed]
    if still:
        lines.append(f"Still open: {', '.join(b.symbol for b in still)}.")
    lines.append(f"All your recorded bets: {total['bets']}, {_inr(total['net_inr'])} net.")
    return "\n".join(lines)


def _noted(conn, key: str) -> bool:
    return conn.execute("select exists(select 1 from my_trade_note where note_key = %s)",
                        (key,)).fetchone()[0]


def _note(conn, key: str, text: str, send) -> bool:
    if _noted(conn, key):
        return False
    if send is not None and send(text) is False:
        return False
    conn.execute("insert into my_trade_note (note_key) values (%s) on conflict do nothing",
                 (key,))
    conn.commit()
    return True


def step(conn, send=None, now: dt.datetime | None = None, transport=None, feed=None) -> str:
    """One check (the intraday job, every 5 minutes): the day's trades, a note on each bet
    opened or closed since, and after the close the day's summary and the market context
    of the bets that need it."""
    now = now or utc_now()
    r = rule()
    if not r["enabled"]:
        return "my trades: off"
    try:
        seen, added = with_account(lambda a: import_day(conn, a, now), transport)
    except AccountError as exc:
        conn.rollback()
        return f"my trades: {exc}"
    from igs.intraday.scanner import token
    from igs.intraday.upstox import FeedError, Upstox
    today = now.astimezone(IST).date()
    out, _ = bets(conn)
    closed = [b for b in out if b.closed and b.net is not None and b.opened_on < today]
    mine = [b for b in out if b.opened_on == today or b.closed_on == today]
    notes = r["notes"] and send is not None
    waiting = now.astimezone(IST).time() >= CLOSE and any(needs_context(b, now) for b in out)
    sent, own = 0, False
    if feed is None and (mine or waiting) and token():
        feed, own = Upstox(token()), True
    try:
        for b in mine if notes else []:
            if b.opened_on == today and b.source == "day" and not _noted(conn, f"open:{b.key}"):
                ctx = dict(b.context)
                if not ctx and feed is not None and b.kind == "intraday" and \
                        b.instrument_key and b.opened_at is not None:
                    try:      # the market before it, as it was when it opened
                        ctx = context(conn, dataclasses.replace(b, closed_on=None,
                                                                closed_at=None),
                                      day_bars(feed, b.instrument_key, today, now),
                                      day_bars(feed, NIFTY, today, now))
                    except FeedError:
                        ctx = {}
                sent += _note(conn, f"open:{b.key}", open_text(b, ctx, closed), send)
            if b.closed and b.closed_on == today:
                sent += _note(conn, f"close:{b.key}", close_text(b, mine), send)
        if feed is not None and waiting:
            try:
                fill_contexts(conn, out, feed, now)
            except FeedError:
                conn.rollback()
    finally:
        if own:
            feed.close()
    if now.astimezone(IST).time() >= CLOSE:
        fill_signals(conn, out)
    if notes and mine and now.astimezone(IST).time() >= dt.time(15, 35):
        sent += _note(conn, f"day:{today}", day_text(today, mine, closed + [
            b for b in mine if b.closed and b.net is not None]), send)
    return (f"my trades: {seen} fills today ({added} new), {len(mine)} bets today"
            + (f", {sent} notes" if sent else ""))
