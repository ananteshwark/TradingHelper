"""Results reactions, tracked forward on paper (docs/RESULTS_DAYS.md). Nothing here orders.

For each company's results for a period, the first NSE financial-results filing
(filing_ref), the reaction is the close-to-close return from the last session before the
filing day to the first session after it, less the average of the liquid stocks (50-session
average traded value at least Rs 50 crore) over the same days. Only liquid companies count.

- A reaction of +5% or better opens a paper buy at the open of the session after that,
  held 21 sessions (to that session's open), measured against the same liquid stocks over
  the same days, after delivery costs (igs.momentum.cost_pct). One noticed after that open
  is recorded as missed, not entered late.
- A reaction of -5% or worse flags the stock for the 21 sessions after. The momentum paper
  track 'results' skips flagged stocks, and AI calls on them show the flag. Nothing real
  is skipped.

In the backtest (research/results), buying after a +5% reaction beat the universe by about
1% over the next month after costs, short of the bar set in advance; stocks after a -5%
reaction lagged by about 0.9%.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math

import polars as pl
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from igs.timeutil import IST, end_of_day_ist, utc_now

REACTION = 5.0                 # % against the liquid stocks
HOLD = 21                      # sessions held
FLAG_SESSIONS = 21             # sessions a bad reaction flags the stock
MIN_TURNOVER = 50e7            # Rs 50 crore a day, 50-session average
TURNOVER_DAYS = 50
PRICE_DAYS = 200               # calendar days of prices loaded
BACKFILL_DAYS = 60             # filings looked at when nothing has been recorded yet
OPEN = dt.time(9, 15)


# --------------------------------------------------------------------------- sessions


def holidays(conn) -> set[dt.date]:
    return {r[0] for r in conn.execute(
        "select holiday_date from trading_holiday where exchange = 'NSE'").fetchall()}


def next_sessions(day: dt.date, n: int, known: list[dt.date], hols: set[dt.date]
                  ) -> list[dt.date]:
    """The n sessions after `day`: the known trading days, then weekdays that are not NSE
    holidays."""
    out = [d for d in known if d > day][:n]
    d = max([day, *out])
    while len(out) < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5 and d not in hols:
            out.append(d)
    return out


# --------------------------------------------------------------------------- reactions


def liquid(px: pl.DataFrame, day: dt.date, min_turnover: float = MIN_TURNOVER) -> list[int]:
    """Companies whose average traded value over the 50 sessions to `day` clears the floor
    and that traded on `day`."""
    t = (px.filter(pl.col("trade_date") <= day).sort("company_id", "trade_date")
         .group_by("company_id", maintain_order=True)
         .agg(pl.col("turnover").tail(TURNOVER_DAYS).mean().alias("t"),
              pl.col("trade_date").last().alias("last")))
    return t.filter((pl.col("t") >= min_turnover) & (pl.col("last") == day))["company_id"].to_list()


class Prices:
    """Adjusted opens and closes by company, for quick lookups."""

    def __init__(self, px: pl.DataFrame):
        self.by: dict[int, tuple[list, list, list]] = {}
        for (cid,), g in px.sort("company_id", "trade_date").group_by(["company_id"],
                                                                       maintain_order=True):
            self.by[cid] = (g["trade_date"].to_list(), g["adj_open"].to_list(),
                            g["adj_close"].to_list())

    def traded(self, cid: int, day: dt.date) -> bool:
        d = self.by.get(cid)
        return bool(d) and day in d[0]

    def close(self, cid: int, day: dt.date) -> float | None:
        d = self.by.get(cid)
        if not d or day not in d[0]:
            return None
        return d[2][d[0].index(day)]

    def value(self, cid: int, day: dt.date, *, use_open: bool = True) -> float | None:
        """The adjusted open on `day` (or close), else the last close before it."""
        d = self.by.get(cid)
        if not d:
            return None
        k = bisect.bisect_right(d[0], day) - 1
        if k < 0:
            return None
        if d[0][k] == day:
            return d[1][k] if use_open else d[2][k]
        return d[2][k]


def reaction(prices: Prices, cid: int, before: dt.date, after: dt.date,
             universe: list[int]) -> tuple[float, float] | None:
    """(the company's %, the universe's average %) from `before`'s close to `after`'s."""
    a, b = prices.close(cid, before), prices.close(cid, after)
    if not (a and b):
        return None
    moves = [(y / x - 1) for x, y in ((prices.close(c, before), prices.close(c, after))
                                       for c in universe) if x and y]
    if not moves:
        return None
    return (b / a - 1) * 100, sum(moves) / len(moves) * 100


# --------------------------------------------------------------------------- database


def releases(conn, since: dt.date, companies: list[int]) -> list[dict]:
    """Each of `companies`' first NSE financial-results filing for a period, filed since
    `since`, not yet recorded: {company_id, symbol, period_end, filed_at}."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            with first as (
                select symbol, period_end, min(filed_at) filed_at from filing_ref
                where exchange = 'NSE' and filing_type = 'financial_results'
                group by symbol, period_end having min(filed_at) >= %s)
            select distinct on (s.company_id, f.period_end) s.company_id, f.symbol,
                   f.period_end, f.filed_at
            from first f
            join security_identifier i on i.id_type = 'NSE_SYMBOL' and i.id_value = f.symbol
                 and i.valid_from <= (f.filed_at at time zone 'Asia/Kolkata')::date
                 and (i.valid_to is null
                      or i.valid_to > (f.filed_at at time zone 'Asia/Kolkata')::date)
            join security s on s.security_id = i.security_id
            where s.company_id = any(%s)
              -- a late or restated filing for an old period is not a results release
              and f.period_end >= (f.filed_at at time zone 'Asia/Kolkata')::date - 120
              and not exists (select 1 from results_reaction r
                              where r.company_id = s.company_id and r.period_end = f.period_end)
            order by s.company_id, f.period_end, f.filed_at""",
                    (end_of_day_ist(since - dt.timedelta(days=1)), companies))
        return cur.fetchall()


def flagged(conn, at: dt.datetime) -> dict[int, dict]:
    """Companies flagged at `at` by a -5% or worse reaction known by then:
    {company_id: {symbol, abnormal_pct, after_date, flag_until}}."""
    day = at.astimezone(IST).date()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select distinct on (company_id) company_id, symbol,
                              abnormal_pct::float8 abnormal_pct, after_date, flag_until
                       from results_reaction
                       where abnormal_pct <= %s and decided_at <= %s
                         and after_date <= %s and flag_until >= %s
                       order by company_id, after_date desc""", (-REACTION, at, day, day))
        return {r["company_id"]: r for r in cur.fetchall()}


def run(conn, *, now: dt.datetime | None = None, min_turnover: float = MIN_TURNOVER,
        notify=None) -> str:
    """Record new reactions, open paper buys and flags, and update the open buys."""
    from igs.momentum import cost_pct, panel, trading_days
    from igs.pit.loader import load_dataset
    from igs.pit.view import PitView
    now = now or utc_now()
    latest = conn.execute("select max(trade_date) from price_eod where exchange = 'NSE'"
                          ).fetchone()[0]
    if latest is None:
        return "no prices loaded"
    first_open = conn.execute("select min(entry_date) from results_trade where status = 'open'"
                              ).fetchone()[0]
    start = min(latest, first_open or latest) - dt.timedelta(days=PRICE_DAYS)
    px = panel(PitView(load_dataset(conn, start, latest), end_of_day_ist(latest)))
    days = trading_days(px)
    hols = holidays(conn)
    recorded = conn.execute("select max(filed_at) from results_reaction").fetchone()[0]
    since = (recorded.astimezone(IST).date() - dt.timedelta(days=30) if recorded
             else latest - dt.timedelta(days=BACKFILL_DAYS))
    prices = Prices(px)
    liquid_on: dict[dt.date, list[int]] = {}
    new_buys, new_flags, n = [], [], 0
    for rel in releases(conn, since, px["company_id"].unique().to_list()):
        filed = rel["filed_at"].astimezone(IST).date()
        before = [d for d in days if d < filed]
        after = [d for d in days if d > filed]
        if not before or not after:
            continue                              # the session after hasn't traded yet
        b0, b1 = before[-1], after[0]
        if b1 not in liquid_on:
            liquid_on[b1] = liquid(px, b1, min_turnover)
        universe = liquid_on[b1]
        if rel["company_id"] not in universe:
            continue
        r = reaction(prices, rel["company_id"], b0, b1, universe)
        if r is None:
            continue
        own, avg = r
        abnormal = own - avg
        flag_until = (next_sessions(b1, FLAG_SESSIONS, days, hols)[-1]
                      if abnormal <= -REACTION else None)
        conn.execute("""insert into results_reaction (company_id, period_end, symbol, filed_at,
                            before_date, after_date, reaction_pct, universe_pct, abnormal_pct,
                            flag_until, decided_at)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (rel["company_id"], rel["period_end"], rel["symbol"], rel["filed_at"], b0, b1,
                      round(own, 3), round(avg, 3), round(abnormal, 3), flag_until, now))
        n += 1
        if flag_until and flag_until >= latest:
            new_flags.append((rel["symbol"], abnormal, flag_until))
        if abnormal >= REACTION:
            entry_date = next_sessions(b1, 1, days, hols)[0]
            on_time = now < dt.datetime.combine(entry_date, OPEN, IST)
            exit_due = next_sessions(entry_date, HOLD, days, hols)[-1]
            conn.execute("""insert into results_trade (company_id, period_end, symbol,
                                abnormal_pct, entry_date, exit_due, status, universe)
                            values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                         (rel["company_id"], rel["period_end"], rel["symbol"], round(abnormal, 3),
                          entry_date, exit_due, "open" if on_time else "missed", Jsonb(universe)))
            if on_time:
                new_buys.append((rel["symbol"], abnormal, entry_date))
    conn.commit()
    updated = _update(conn, prices, days, hols, latest, cost_pct)
    if notify and (new_buys or new_flags):
        notify(message(new_buys, new_flags))
    return (f"{n} new reaction(s), {len(new_buys)} paper buy(s), {len(new_flags)} flag(s); "
            f"{updated} paper buy(s) priced to {latest:%d %b %Y}")


def _update(conn, prices: Prices, days, hols, latest, cost_pct) -> int:
    """Price the open paper buys: entry at the open of the session after the reaction, exit
    at the open 21 sessions on, or marked to the latest close until then. The sessions are
    worked out again from the known trading days, in case one was a holiday not listed."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select t.company_id, t.period_end, r.after_date, t.universe
                       from results_trade t join results_reaction r using (company_id, period_end)
                       where t.status = 'open' and t.entry_date <= %s""", (latest,))
        rows = cur.fetchall()
    n = 0
    for t in rows:
        cid = t["company_id"]
        entry_date = next_sessions(t["after_date"], 1, days, hols)[0]
        exit_due = next_sessions(entry_date, HOLD, days, hols)[-1]
        key = (cid, t["period_end"])
        if entry_date > latest:
            conn.execute("update results_trade set entry_date = %s, exit_due = %s "
                         "where company_id = %s and period_end = %s",
                         (entry_date, exit_due, *key))
            continue
        if not prices.traded(cid, entry_date):
            conn.execute("update results_trade set status = 'no data', entry_date = %s, "
                         "exit_due = %s, updated_at = now() "
                         "where company_id = %s and period_end = %s",
                         (entry_date, exit_due, *key))
            continue
        entry = prices.value(cid, entry_date)
        done = exit_due <= latest
        end = exit_due if done else latest
        exit_price = prices.value(cid, end, use_open=done)
        rets = []
        for c in t["universe"]:
            a, b = prices.value(c, entry_date), prices.value(c, end, use_open=done)
            if a and b:
                rets.append(b / a - 1)
        ret = (exit_price / entry - 1) * 100
        uni = sum(rets) / len(rets) * 100 if rets else 0.0
        cost = cost_pct(entry)
        conn.execute("""update results_trade set entry_date = %s, exit_due = %s, entry = %s,
                            exit_date = %s, exit = %s, return_pct = %s, universe_pct = %s,
                            cost_pct = %s, net_excess_pct = %s, status = %s, updated_at = now()
                        where company_id = %s and period_end = %s""",
                     (entry_date, exit_due, entry, end, exit_price, round(ret, 3), round(uni, 3),
                      round(cost, 3), round(ret - uni - cost, 3), "closed" if done else "open",
                      *key))
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- reporting


def record(conn) -> dict:
    """The closed paper buys' summary, the open ones, and the stocks flagged now."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select symbol, abnormal_pct::float8 abnormal_pct, entry_date, exit_due,
                              exit_date, status, return_pct::float8 return_pct,
                              universe_pct::float8 universe_pct, cost_pct::float8 cost_pct,
                              net_excess_pct::float8 net_excess_pct
                       from results_trade order by entry_date desc, symbol""")
        trades = cur.fetchall()
    closed = [t["net_excess_pct"] for t in trades if t["status"] == "closed"]
    n = len(closed)
    mean = sum(closed) / n if n else None
    sd = (math.sqrt(sum((x - mean) ** 2 for x in closed) / (n - 1)) if n > 1 else None)
    return {"trades": trades,
            "closed": {"n": n, "mean_net_excess_pct": mean,
                       "win": sum(x > 0 for x in closed) / n if n else None,
                       "t": mean / sd * math.sqrt(n) if sd else None},
            "flagged": sorted(flagged(conn, utc_now()).values(), key=lambda r: r["after_date"],
                              reverse=True)}


def ai_call_comparison(conn, calls: list[dict]) -> list[dict]:
    """AI buy calls made while the stock was flagged, against the others, by horizon:
    [{horizon, group, calls, mean_excess_pct}] from the AI calls record."""
    flags = conn.execute("""select company_id, after_date, flag_until, decided_at
                            from results_reaction where abnormal_pct <= %s""",
                         (-REACTION,)).fetchall()
    by = {}
    for cid, a, u, d in flags:
        by.setdefault(cid, []).append((a, u, d))

    def is_flagged(c):
        made = c["created_at"]
        day = made.astimezone(IST).date()
        return any(a <= day <= u and d <= made for a, u, d in by.get(c["company_id"], ()))

    out: dict[tuple[str, str], list[float]] = {}
    for c in calls:
        if c["action"] != "buy":
            continue
        group = "after bad results" if is_flagged(c) else "other buy calls"
        for name, h in (c.get("outcome") or {}).get("horizons", {}).items():
            if h and h.get("excess_pct") is not None:
                out.setdefault((name, group), []).append(h["excess_pct"])
    return [{"horizon": h, "group": g, "calls": len(v),
             "mean_excess_pct": round(sum(v) / len(v), 1)} for (h, g), v in sorted(out.items())]


def message(buys: list[tuple], flags: list[tuple]) -> str:
    lines = ["RESULTS REACTIONS (paper only; no orders)"]
    for sym, ab, entry in buys:
        lines.append(f"Paper buy {sym}: results moved it {ab:+.1f}% against the market; "
                     f"enters at the {entry:%d %b} open, held 21 sessions.")
    for sym, ab, until in flags:
        lines.append(f"Flag {sym}: results moved it {ab:+.1f}% against the market; momentum's "
                     f"'skipping bad results' track and AI calls note it until {until:%d %b}.")
    lines.append("In the backtest, stocks after a +5% reaction beat the market by about 1% "
                 "over the next month after costs (not proven); after −5%, they lagged by "
                 "about 0.9%. Results days page in the app.")
    return "\n".join(lines)


def step(conn) -> str:
    """The daily job's step, with Telegram for new buys and flags."""
    from igs.alerts.delivery import send_telegram
    return run(conn, notify=send_telegram)
