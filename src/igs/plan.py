"""The Plan: a paper portfolio of momentum stocks, gold and cash, kept in rupees with whole
shares, whose orders for the next open are sent on Telegram. Nothing here places an order.

Why this mix (docs/PLAN.md): risk-weighted momentum was the one approach that passed in
both halves of the 2013-2026 tests (docs/INSTITUTIONAL_ALGOS.md), but it fell 47% at its
worst. Gold hardly moves with it, and cash lets the plan buy after a fall. On the same data
60% momentum, 25% gold and 15% cash made about 20% a year before tax and 17% after, falling
at most 30% (illustrative, not a pre-registered test).

The rules (shares and amounts from config/plan.yaml):
- Momentum part: at each month's last close, the momentum rule's ranking (igs.momentum: the
  Nifty 200 stocks trading at least Rs 50 crore a day, by their return from 12 months to 1
  month back), less stocks flagged by a bad results reaction (igs.results_drift) and stocks
  with a tripped 'reject' red flag in the latest score run. The best ten, each weighted by
  the inverse of its 63-session volatility, none above 15% of the part. A stock that leaves
  the ten is sold and a new one bought; one that stays is resized only when it is more than
  25% off its target.
- All three parts are brought back to their shares at each quarter's first session
  (January, April, July, October), and at the next open whenever gold or cash closes more
  than five points from its share, after a crash or when money is added.
- Orders are whole shares worked out at a close for the next open. The paper record fills
  them at that open after charges and marks the plan to each close. Cash earns 6% a year,
  dividends are credited on the ex-date, and splits and bonuses change the share counts.
  Money added or withdrawn buys or sells units, so it never counts as a return.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math

import polars as pl
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs.config import PlanConfig, load_plan
from igs.momentum import BENCHMARK, MIN_TURNOVER, members, rank, risk_picks, trading_days
from igs.timeutil import IST, end_of_day_ist

RESIZE_MONTHLY = 0.25      # a kept stock is resized at a monthly change when this far off
RESIZE_ALL = 0.10          # ... and when all three parts are rebalanced
GOLD_RESIZE = 0.02
QUARTER_MONTHS = (1, 4, 7, 10)
START_UNIT = 100.0
HISTORY_DAYS = 420         # calendar days of prices loaded before the last recorded session
SLIPPAGE = {"stock": 0.0005, "gold": 0.0002}     # each side, of the order's value
OPEN = dt.time(9, 15)
REASONS = {
    "start": "Starting the plan: {core:g}% momentum stocks, {gold:g}% gold, {cash:g}% cash.",
    "monthly": "Monthly change of momentum stocks; gold and cash stay as they are.",
    "quarterly": "Quarterly rebalance: all three parts back to {core:g}/{gold:g}/{cash:g}.",
    "drift": "Gold or cash closed more than {drift:g} points from its share: all three parts "
             "back to {core:g}/{gold:g}/{cash:g}.",
}


def charges(kind: str, side: str, value: float) -> float:
    """Rupee costs of one delivery order of `value`: STT (stocks only), stamp duty on buys,
    exchange and SEBI fees, Rs 20 brokerage, GST, the DP charge on sells, and slippage. A
    stock bought and sold costs what igs.momentum.cost_pct says."""
    if value <= 0:
        return 0.0
    exch, sebi, brokerage = 0.0000297 * value, 0.000001 * value, 20.0
    total = exch + sebi + brokerage + 0.18 * (brokerage + exch + sebi) + SLIPPAGE[kind] * value
    if kind == "stock":
        total += 0.001 * value
    total += 0.00015 * value if side == "buy" else 15.93
    return round(total, 2)


def inr(v: float) -> str:
    """Rupees with Indian digit grouping: 1023450 -> 'Rs 10,23,450' (with the rupee sign)."""
    s = str(int(round(abs(v))))
    if len(s) > 3:
        head, groups = s[:-3], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join([head, *groups, s[-3:]])
    return ("-" if v < 0 else "") + "₹" + s


def cap_weights(weights: list[float], cap: float) -> list[float]:
    """Weights summing to 1 with none above `cap`, the excess spread over the others in
    proportion."""
    w = [x / sum(weights) for x in weights]
    for _ in range(len(w)):
        over = [x > cap + 1e-12 for x in w]
        if not any(over):
            break
        excess = sum(x - cap for x, o in zip(w, over, strict=True) if o)
        rest = sum(x for x, o in zip(w, over, strict=True) if not o)
        w = [cap if o else x + excess * x / rest for x, o in zip(w, over, strict=True)]
    return w


# --------------------------------------------------------------------------- prices


class Book:
    """Raw opens and closes and the split/bonus factor by company id (and 'gold') and day."""

    def __init__(self, px: pl.DataFrame, gold: list[tuple]):
        self.rows: dict = {}
        for cid, d, o, c, f in px.select("company_id", "trade_date", "open", "close",
                                          "cum_factor").iter_rows():
            self.rows.setdefault(cid, {})[d] = (o, c, f or 1.0)
        for d, o, c in gold:
            self.rows.setdefault("gold", {})[d] = (o, c, 1.0)
        self.days = {k: sorted(v) for k, v in self.rows.items()}

    def _on_or_before(self, key, day):
        days = self.days.get(key, [])
        i = bisect.bisect_right(days, day)
        return self.rows[key][days[i - 1]] if i else None

    def open(self, key, day) -> float | None:
        row = self.rows.get(key, {}).get(day)
        return row[0] if row and row[0] else None

    def close(self, key, day) -> float | None:
        row = self._on_or_before(key, day)
        return row[1] if row else None

    def factor(self, key, day) -> float:
        row = self._on_or_before(key, day)
        return row[2] if row else 1.0


def _load(conn, cfg: PlanConfig, start: dt.date, latest: dt.date):
    from igs.factors.base import index_closes, primary_prices
    from igs.pit.loader import load_dataset
    from igs.pit.view import PitView
    view = PitView(load_dataset(conn, start, latest), end_of_day_ist(latest))
    px = primary_prices(view).select(
        "company_id", "symbol", "trade_date", "open", "close", "adj_open", "adj_close",
        "cum_factor", pl.col("turnover_inr").fill_null(pl.col("close") * pl.col("volume"))
        .alias("turnover")).sort("company_id", "trade_date")
    gold = conn.execute("""select trade_date, open::float8, close::float8 from price_eod
                           where exchange = 'NSE' and symbol = %s and series = 'EQ'
                             and trade_date between %s and %s order by trade_date""",
                        (cfg.gold_symbol, start, latest)).fetchall()
    nifty = dict(index_closes(view, BENCHMARK).iter_rows())
    return px, Book(px, gold), nifty


# --------------------------------------------------------------------------- the momentum part


def red_flags(conn, day: dt.date) -> dict[int, str]:
    """Companies with a tripped 'reject' red flag in the latest score run as of `day`'s
    close: {company_id: the flags, in words}."""
    from igs.score.red_flags import LABELS
    rows = conn.execute("""select company_id, flag from red_flag_result
                           where run_id = (select run_id from score_run where as_of <= %s
                                           order by as_of desc, run_id desc limit 1)
                             and status = 'tripped' and severity = 'reject'
                           order by company_id, flag""", (end_of_day_ist(day),)).fetchall()
    out: dict[int, list[str]] = {}
    for cid, flag in rows:
        out.setdefault(cid, []).append(LABELS.get(flag, flag.replace("_", " ")))
    return {c: ", ".join(v) for c, v in out.items()}


def core_picks(conn, cfg: PlanConfig, px: pl.DataFrame, day: dt.date,
               min_turnover: float = MIN_TURNOVER) -> tuple[list[dict], dict, str]:
    """(picks, why, note) at `day`'s close. picks: the best `cfg.stocks` of the momentum
    ranking after the exclusions, each with its capped inverse-volatility weight. why:
    {company_id: words} for every ranked or excluded company, used to say why a holding was
    sold."""
    from igs.results_drift import flagged
    ids, _ = members(conn, day)
    if not ids:
        return [], {}, "No Nifty 200 members loaded yet, so no momentum stocks."
    ranking = rank(px, ids, day, min_turnover=min_turnover)
    results = flagged(conn, end_of_day_ist(day))
    flags = red_flags(conn, day)
    why = {r["company_id"]: f"rank {r['rank']} now" for r in ranking}
    eligible, skipped = [], []
    for r in ranking:
        cid = r["company_id"]
        if cid in results:
            why[cid] = f"bad results reaction ({results[cid]['abnormal_pct']:+.1f}%)"
        elif cid in flags:
            why[cid] = f"red flag: {flags[cid]}"
        else:
            eligible.append(r)
            continue
        if len(eligible) < cfg.stocks:
            skipped.append(f"{r['symbol']} ({why[cid]})")
    held, _, _ = risk_picks(px, day, eligible, cfg.stocks)
    weights = cap_weights([h["weight"] for h in held], cfg.max_stock_pct / 100) if held else []
    picks = [{**h, "weight": w} for h, w in zip(held, weights, strict=True)]
    note = ("Left out: " + ", ".join(skipped) + "." if skipped else "")
    if len(picks) < cfg.stocks:
        note = (note + f" Only {len(picks)} stocks qualified.").strip()
    return picks, why, note


# --------------------------------------------------------------------------- the ledger


def _value(book: Book, holdings: list[dict], day: dt.date) -> float:
    return sum(h["shares"] * (book.close(_key(h), day) or 0.0) for h in holdings)


def _key(h: dict):
    return h["company_id"] if h["kind"] == "stock" else "gold"


def _last_day(conn) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select trade_date, cash_inr::float8 cash, holdings, value_inr::float8 value,
                              units::float8 units, unit_value::float8 unit_value
                       from plan_day order by trade_date desc limit 1""")
        return cur.fetchone()


def _record_day(conn, book: Book, nifty: dict, day: dt.date, cash: float,
                holdings: list[dict], units: float, flows: float = 0.0,
                charges_inr: float = 0.0, dividends: float = 0.0) -> dict:
    shown = []
    for h in holdings:
        close = book.close(_key(h), day)
        shown.append({**h, "close": close, "value_inr": round(h["shares"] * (close or 0), 2)})
    value = cash + sum(h["value_inr"] for h in shown)
    row = {"trade_date": day, "cash": cash, "holdings": shown, "value": value, "units": units,
           "unit_value": value / units}
    conn.execute("""insert into plan_day (trade_date, cash_inr, holdings, value_inr, units,
                        unit_value, nifty_close, flows_inr, charges_inr, dividends_inr)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                 (day, round(cash, 2), Jsonb(shown), round(value, 2), units,
                  round(value / units, 6), nifty.get(day), round(flows, 2),
                  round(charges_inr, 2), round(dividends, 2)))
    return row


def _dividends(conn, ids: list[int], after: dt.date, upto: dt.date) -> dict[int, float]:
    if not ids:
        return {}
    rows = conn.execute("""select s.company_id, sum(ca.cash_per_share)::float8
                           from corporate_action ca join security s using (security_id)
                           where ca.action_type = 'dividend' and s.company_id = any(%s)
                             and ca.ex_date > %s and ca.ex_date <= %s
                           group by s.company_id""", (ids, after, upto)).fetchall()
    return dict(rows)


def _session(conn, cfg: PlanConfig, book: Book, nifty: dict, prev: dict, day: dt.date
             ) -> dict:
    """The plan through `day`: cash interest, splits and bonuses, dividends, money added or
    withdrawn, orders filled at the open, and the mark at the close."""
    holdings = [dict(h) for h in prev["holdings"]]
    for h in holdings:
        h.pop("close", None)
        h.pop("value_inr", None)
    days = (day - prev["trade_date"]).days
    cash = prev["cash"] * (1 + cfg.cash_return_pct / 100) ** (days / 365)
    for h in holdings:
        if h["kind"] == "stock":
            before, now = book.factor(h["company_id"], prev["trade_date"]), book.factor(
                h["company_id"], day)
            if before and now and abs(now / before - 1) > 1e-9:
                h["shares"] = math.floor(h["shares"] * now / before + 1e-6)
    paid = _dividends(conn, [h["company_id"] for h in holdings if h["kind"] == "stock"],
                      prev["trade_date"], day)
    dividends = sum(h["shares"] * paid.get(h["company_id"], 0.0) for h in holdings
                    if h["kind"] == "stock")
    cash += dividends
    units, flows = prev["units"], 0.0
    for flow_id, amount in conn.execute(
            """select flow_id, amount_inr::float8 from plan_flow
               where applied_on is null and created_at < %s order by flow_id""",
            (dt.datetime.combine(day, OPEN, tzinfo=IST),)).fetchall():
        units += amount / prev["unit_value"]
        cash += amount
        flows += amount
        conn.execute("update plan_flow set applied_on = %s where flow_id = %s", (day, flow_id))
    cost = 0.0
    for reb in conn.execute("""select signal_date, orders from plan_rebalance
                               where filled_on is null and signal_date < %s
                               order by signal_date""", (day,)).fetchall():
        cash, spent, orders = _fill(book, holdings, cash, reb[1], day)
        cost += spent
        conn.execute("update plan_rebalance set orders = %s, filled_on = %s where signal_date = %s",
                     (Jsonb(orders), day, reb[0]))
    holdings = [h for h in holdings if h["shares"] > 0]
    return _record_day(conn, book, nifty, day, cash, holdings, units, flows, cost, dividends)


def _fill(book: Book, holdings: list[dict], cash: float, orders: list[dict], day: dt.date
          ) -> tuple[float, float, list[dict]]:
    """Fill `orders` at `day`'s open, sells first; a buy is cut to what the cash pays for.
    Returns (cash, charges, the orders with their fills)."""
    out, spent = [], 0.0
    by_key = {_key(h): h for h in holdings}
    for o in sorted(orders, key=lambda o: o["side"] != "sell"):
        o = dict(o)
        key = o["company_id"] if o["kind"] == "stock" else "gold"
        price = book.open(key, day)
        h = by_key.get(key)
        if price is None:
            o.update(filled=0, fill_price=None, charges_inr=0.0, fill_note="no trade at the open")
        elif o["side"] == "sell":
            n = min(o["shares"], h["shares"] if h else 0)
            fee = charges(o["kind"], "sell", n * price)
            cash += n * price - fee
            if h:
                h["shares"] -= n
            o.update(filled=n, fill_price=price, charges_inr=fee)
            spent += fee
        else:
            n = min(o["shares"], max(0, math.floor(cash / (price * 1.003))))
            fee = charges(o["kind"], "buy", n * price)
            cash -= n * price + fee
            if n and h:
                h["shares"] += n
            elif n:
                h = {"symbol": o["symbol"], "company_id": o["company_id"], "kind": o["kind"],
                     "shares": n}
                holdings.append(h)
                by_key[key] = h
            o.update(filled=n, fill_price=price, charges_inr=fee)
            if n < o["shares"]:
                o["fill_note"] = "cut to the cash available"
            spent += fee
        out.append(o)
    return cash, spent, out


# --------------------------------------------------------------------------- orders


def _due(cfg: PlanConfig, state: dict, book: Book, day: dt.date, nxt: dt.date
         ) -> tuple[str, bool] | None:
    """(reason, all parts?) if orders are due after `day`'s close, else None."""
    value = state["value"]
    gold = sum(h["value_inr"] for h in state["holdings"] if h["kind"] == "gold")
    drift = value > 0 and (abs(gold / value * 100 - cfg.gold_pct) > cfg.drift_points
                           or abs(state["cash"] / value * 100 - cfg.cash_pct)
                           > cfg.drift_points)
    if (nxt.year, nxt.month) != (day.year, day.month):
        quarter = nxt.month in QUARTER_MONTHS
        return ("quarterly" if quarter else "monthly"), quarter or drift
    return ("drift", True) if drift else None


def make_orders(conn, cfg: PlanConfig, px: pl.DataFrame, book: Book, state: dict,
                day: dt.date, nxt: dt.date, reason: str, all_parts: bool,
                min_turnover: float = MIN_TURNOVER) -> dict:
    """Work out and store the orders for `nxt`'s open from the plan at `day`'s close."""
    value = state["value"]
    stocks = {h["company_id"]: h for h in state["holdings"] if h["kind"] == "stock"}
    stock_value = sum(h["value_inr"] for h in stocks.values())
    note, why = "", {}
    if reason == "drift":
        picks = [{"company_id": c, "symbol": h["symbol"], "rank": None,
                  "weight": h["value_inr"] / stock_value} for c, h in stocks.items()
                 if stock_value > 0]
    else:
        picks, why, note = core_picks(conn, cfg, px, day, min_turnover)
        if not picks and stocks:              # no ranking: keep what is held
            picks = [{"company_id": c, "symbol": h["symbol"], "rank": None,
                      "weight": h["value_inr"] / stock_value} for c, h in stocks.items()]
    core = value * cfg.core_pct / 100 if all_parts or not stocks else stock_value
    cap = core * cfg.max_stock_pct / 100
    band = RESIZE_ALL if all_parts else RESIZE_MONTHLY
    orders, targets, too_dear = [], [], []
    for p in picks:
        cid, price = p["company_id"], book.close(p["company_id"], day)
        target = core * p["weight"]
        have = stocks[cid]["shares"] if cid in stocks else 0
        if not price:
            continue
        want = min(math.floor(target / price + 0.5), math.floor(cap / price))
        if have and abs(want - have) * price <= band * target:
            want = have
        targets.append({"symbol": p["symbol"], "company_id": cid, "kind": "stock",
                        "rank": p.get("rank"), "weight_pct": round(p["weight"] * 100, 1),
                        "value_inr": round(target)})
        if want == 0 and not have:
            too_dear.append(p["symbol"])
        elif want != have:
            new = not have
            orders.append({"symbol": p["symbol"], "company_id": cid, "kind": "stock",
                           "side": "buy" if want > have else "sell",
                           "shares": abs(want - have), "price": price,
                           "why": (f"new, rank {p['rank']}" if new and p.get("rank")
                                   else "to its target" if all_parts else
                                   "more than 25% off its target")})
    chosen = {p["company_id"] for p in picks}
    for cid, h in stocks.items():
        if cid not in chosen:
            orders.append({"symbol": h["symbol"], "company_id": cid, "kind": "stock",
                           "side": "sell", "shares": h["shares"],
                           "price": book.close(cid, day),
                           "why": why.get(cid, "no longer ranked (left the Nifty 200 or "
                                               "trades under ₹50 crore a day)")})
    gold_price = book.close("gold", day)
    if all_parts and gold_price:
        target = value * cfg.gold_pct / 100
        have = sum(h["shares"] for h in state["holdings"] if h["kind"] == "gold")
        want = math.floor(target / gold_price + 0.5)
        targets.append({"symbol": cfg.gold_symbol, "company_id": None, "kind": "gold",
                        "rank": None, "weight_pct": cfg.gold_pct, "value_inr": round(target)})
        if abs(want - have) * gold_price > GOLD_RESIZE * target:
            orders.append({"symbol": cfg.gold_symbol, "company_id": None, "kind": "gold",
                           "side": "buy" if want > have else "sell",
                           "shares": abs(want - have), "price": gold_price,
                           "why": "gold to its share"})
    elif all_parts:
        note = (note + f" No {cfg.gold_symbol} price loaded, so no gold order.").strip()
    if too_dear:
        note = (note + f" One share costs more than the target for {', '.join(too_dear)}; "
                "that money stays in cash.").strip()
    row = {"signal_date": day, "session_date": nxt, "reason": reason, "all_parts": all_parts,
           "targets": targets, "orders": orders, "note": note}
    conn.execute("""insert into plan_rebalance (signal_date, session_date, reason, all_parts,
                        targets, orders, note) values (%s, %s, %s, %s, %s, %s, %s)""",
                 (day, nxt, reason, all_parts, Jsonb(targets), Jsonb(orders), note))
    return row


# --------------------------------------------------------------------------- the daily run


def run(conn, *, cfg: PlanConfig | None = None, min_turnover: float = MIN_TURNOVER,
        notify=None) -> str:
    """Start the plan on the first run, then take each new session in turn: fill the orders
    due at its open, mark it to its close, and work out the next open's orders when they are
    due. Sends the latest orders not yet filled. Returns a one-line summary."""
    from igs.results_drift import holidays, next_sessions
    cfg = cfg or load_plan()
    latest = conn.execute("select max(trade_date) from price_eod where exchange = 'NSE'"
                          ).fetchone()[0]
    if latest is None:
        return "no prices loaded"
    state = _last_day(conn)
    if state and state["trade_date"] >= latest:
        return f"up to date at {latest:%d %b %Y}"
    since = state["trade_date"] if state else latest
    px, book, nifty = _load(conn, cfg, since - dt.timedelta(days=HISTORY_DAYS), latest)
    days = trading_days(px)
    hols = holidays(conn)
    made = []
    if state is None:
        if not book.days.get("gold"):
            return f"not started: no {cfg.gold_symbol} prices loaded"
        state = _record_day(conn, book, nifty, latest, cfg.capital_inr, [],
                            cfg.capital_inr / START_UNIT)
        nxt = next_sessions(latest, 1, days, hols)[0]
        made.append(make_orders(conn, cfg, px, book, state, latest, nxt, "start", True,
                                min_turnover))
    for day in [d for d in days if d > state["trade_date"]]:
        state = _session(conn, cfg, book, nifty, state, day)
        nxt = next_sessions(day, 1, days, hols)[0]
        due = _due(cfg, state, book, day, nxt)
        if due:
            made.append(make_orders(conn, cfg, px, book, state, day, nxt, *due, min_turnover))
    conn.commit()
    pending = [m for m in made if m["session_date"] > latest]
    if pending and notify is not None:
        notify(message(conn, pending[-1], cfg))
    return (f"value {inr(state['value'])} at {latest:%d %b %Y}"
            + (f"; orders for the {pending[-1]['session_date']:%d %b} open" if pending else ""))


def step(conn) -> str:
    """The daily job's step, with Telegram for the orders."""
    from igs.alerts.delivery import send_paper
    return run(conn, notify=send_paper)


# --------------------------------------------------------------------------- reporting


def start(conn) -> dict | None:
    """The first recorded day: {trade_date, value, nifty_close}."""
    row = conn.execute("""select trade_date, value_inr::float8, nifty_close::float8 from plan_day
                          order by trade_date limit 1""").fetchone()
    return dict(zip(("trade_date", "value", "nifty_close"), row, strict=True)) if row else None


def summary(conn) -> dict | None:
    """The plan now: value, the parts' shares, return since the start and the Nifty 50's."""
    last, first = _last_day(conn), start(conn)
    if last is None:
        return None
    nifty = conn.execute("""select nifty_close::float8 from plan_day where nifty_close is not null
                            order by trade_date desc limit 1""").fetchone()
    value = last["value"]
    parts = {"stocks": sum(h["value_inr"] for h in last["holdings"] if h["kind"] == "stock"),
             "gold": sum(h["value_inr"] for h in last["holdings"] if h["kind"] == "gold"),
             "cash": last["cash"]}
    return {**last, "start": first["trade_date"],
            "since_pct": (last["unit_value"] / START_UNIT - 1) * 100,
            "nifty_pct": ((nifty[0] / first["nifty_close"] - 1) * 100
                          if nifty and first["nifty_close"] else None),
            "parts": parts,
            "shares": {k: (v / value * 100 if value else None) for k, v in parts.items()},
            "added_inr": conn.execute("select coalesce(sum(flows_inr), 0)::float8 from plan_day"
                                      ).fetchone()[0]}


def history(conn) -> list[dict]:
    rows = conn.execute("""select trade_date, value_inr::float8, unit_value::float8,
                                  nifty_close::float8 from plan_day order by trade_date"""
                        ).fetchall()
    return [dict(zip(("trade_date", "value", "unit_value", "nifty_close"), r, strict=True))
            for r in rows]


def rebalances(conn, limit: int = 24) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select signal_date, session_date, reason, all_parts, targets, orders, note,
                              filled_on from plan_rebalance order by signal_date desc limit %s""",
                    (limit,))
        return cur.fetchall()


def add_flow(conn, amount: float, note: str = "") -> None:
    """Money added (+) or withdrawn (-), applied at the next session's open."""
    if not amount:
        raise ValueError("amount must not be zero")
    conn.execute("insert into plan_flow (amount_inr, note) values (%s, %s)", (amount, note))


def message(conn, reb: dict, cfg: PlanConfig | None = None) -> str:
    """The Telegram note with the orders for the next open."""
    cfg = cfg or load_plan()
    s = summary(conn)
    lines = [f"THE PLAN · orders for the {reb['session_date']:%d %b} open "
             "(paper record; place them yourself if you follow it)"]
    if s and reb["reason"] == "start":
        lines.append(f"Capital {inr(s['value'])}.")
    elif s:
        lines.append(f"Value {inr(s['value'])} at the {reb['signal_date']:%d %b} close: "
                     f"{s['since_pct']:+.1f}% since {s['start']:%d %b %Y}"
                     + (f" (Nifty 50 {s['nifty_pct']:+.1f}%)" if s["nifty_pct"] is not None
                        else "") + ".")
    lines.append(REASONS[reb["reason"]].format(core=cfg.core_pct, gold=cfg.gold_pct,
                                               cash=cfg.cash_pct, drift=cfg.drift_points)
                 + (" Gold or cash had also drifted, so all three parts are rebalanced."
                    if reb["reason"] == "monthly" and reb["all_parts"] else ""))
    orders = sorted(reb["orders"], key=lambda o: (o["side"] != "sell", o["symbol"]))
    for o in orders:
        lines.append(f"{o['side'].upper()} {o['shares']} {o['symbol']} "
                     f"(about {inr(o['shares'] * (o['price'] or 0))}): {o['why']}")
    if not orders:
        lines.append("No orders: the holdings are close enough to their targets.")
    if s:
        after = dict(s["parts"])
        for o in orders:
            amount = o["shares"] * (o["price"] or 0) * (1 if o["side"] == "buy" else -1)
            after["stocks" if o["kind"] == "stock" else "gold"] += amount
            after["cash"] -= amount
        total = sum(after.values())
        lines.append(("After them: " if orders else "Now: ") + ", ".join(
            f"{k} {inr(v)} ({v / total * 100:.0f}%)" for k, v in after.items()) + ".")
    if reb["note"]:
        lines.append(reb["note"])
    lines.append("Prices are the last close and quantities whole shares. Spread the orders over "
                 "the first hour rather than placing them at 9:15.")
    lines.append("https://stocks.ednis.ai/")
    return "\n".join(lines)
