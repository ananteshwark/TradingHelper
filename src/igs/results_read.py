"""How a newly filed quarterly report reads from its own figures, and whether the price rose
after reports that read positive. Telegram gets a quarterly report only when it reads
positive (config/alerts.yaml, rules.quarterly_results; igs.alerts.operations).

The read compares the quarter with the same quarter a year earlier, from the filed figures
known when the report was filed:

- revenue (interest earned for banks), and profit attributable to the owners (net profit
  where that isn't reported), each the year-on-year change;
- the surprise: the profit change on a year earlier, less its average over up to eight
  earlier quarters (at least four), in standard deviations of it there. This is the
  standardized unexpected earnings of the post-earnings-drift studies (Foster, Olsen and
  Shevlin 1984; Bernard and Thomas 1989), with last year's quarter plus the usual change as
  the expectation: without analysts' estimates, the usual stand-in for "better than
  expected". Profit rather than EPS, so splits and bonuses don't distort it.

Positive: a profit, revenue up at least min_revenue_growth_pct, profit up at least
min_profit_growth_pct (or a loss a year earlier turned into a profit), and a surprise of at
least min_surprise where it can be worked out. Negative: profit fell, a loss that didn't
narrow, or a surprise of -min_surprise or worse. Mixed: the rest. Unknown: the quarter or the
year-earlier quarter isn't loaded.

The rule is not validated: the app has no history of results figures and reactions to test it
on. Each read is kept (results_read) with the stock's move against the Nifty 500 from the last
close before the filing, so the Paper tracking page's Results days tab shows whether positive
reads were followed by rises.
"""
from __future__ import annotations

import datetime as dt
import statistics

from psycopg.rows import dict_row

from igs.timeutil import IST

DEFAULTS = {"enabled": True, "only_positive": True, "min_revenue_growth_pct": 5,
            "min_profit_growth_pct": 15, "min_surprise": 1.0}
TOP_LINE = ("revenue", "interest_earned")
PROFIT = ("pat_owners", "pat")
SURPRISE_QUARTERS = 8          # earlier year-on-year changes the surprise is scaled by
MIN_SURPRISE_QUARTERS = 4
MOVES = {"next_session": 1, "sessions_21": 21}   # sessions after the last close before filing
VERDICTS = ("positive", "mixed", "negative", "unknown")
RECORD_READS = 300             # the latest positive reads a message's record covers
FALLEN_PCT = -30.0             # this far below the one-year high: a possible bottom


def rule(cfg=None) -> dict:
    """rules.quarterly_results in config/alerts.yaml over DEFAULTS."""
    if cfg is None:
        from igs.config import load_alerts
        cfg = load_alerts()
    return {**DEFAULTS, **(cfg.rules.get("quarterly_results") or {})}


def quarter_back(day: dt.date, quarters: int) -> dt.date:
    """The month end `quarters` quarters before `day`'s month: 30 Jun 2026, 4 -> 30 Jun 2025."""
    months = day.year * 12 + day.month - 1 - 3 * quarters
    year, month = divmod(months, 12)
    return dt.date(year + (month + 1) // 12, (month + 1) % 12 + 1, 1) - dt.timedelta(days=1)


def figures(conn, company_id: int, basis: str, period_end: dt.date,
            known_at: dt.datetime | None) -> dict[dt.date, dict[str, float]]:
    """The company's quarterly revenue and profit on `basis` for the three years to
    `period_end`, each the latest figure filed by `known_at` (all filed so far without it)."""
    with conn.cursor() as cur:
        cur.execute("""
            select distinct on (period_end, concept) period_end, concept, value::float8
            from fundamental_fact
            where company_id = %s and statement_basis = %s and period_type = 'Q'
              and concept = any(%s) and period_end between %s and %s
              and (%s::timestamptz is null or filed_at <= %s)
            order by period_end, concept, filed_at desc, fact_id desc""",
                    (company_id, basis, [*TOP_LINE, *PROFIT],
                     quarter_back(period_end, SURPRISE_QUARTERS + 4), period_end,
                     known_at, known_at))
        out: dict[dt.date, dict[str, float]] = {}
        for day, concept, value in cur.fetchall():
            out.setdefault(day, {})[concept] = value
        return out


def _pick(quarters: dict, now: dt.date, then: dt.date, concepts: tuple[str, ...]):
    """(concept, value now, value a year earlier): the first concept the quarter has both
    years, else the first it has this year (a year earlier None)."""
    found = None
    for c in concepts:
        if c in quarters.get(now, {}):
            if c in quarters.get(then, {}):
                return c, quarters[now][c], quarters[then][c]
            found = found or (c, quarters[now][c], None)
    return found


def surprise(quarters: dict, period_end: dt.date, concept: str) -> float | None:
    """The profit change on a year earlier less its mean in up to SURPRISE_QUARTERS earlier
    quarters (at least MIN_SURPRISE_QUARTERS), over its standard deviation there."""
    def change(k: int) -> float | None:
        a = quarters.get(quarter_back(period_end, k), {}).get(concept)
        b = quarters.get(quarter_back(period_end, k + 4), {}).get(concept)
        return None if a is None or b is None else a - b

    now = change(0)
    past = [c for c in (change(k) for k in range(1, SURPRISE_QUARTERS + 1)) if c is not None]
    if now is None or len(past) < MIN_SURPRISE_QUARTERS:
        return None
    sd = statistics.stdev(past)
    return (now - statistics.fmean(past)) / sd if sd > 0 else None


def _crore(v: float) -> str:
    return f"Rs {v / 1e7:,.1f} crore"


def judge(quarters: dict, period_end: dt.date, r: dict) -> dict:
    """The read of the quarter ended `period_end` from `quarters` (figures), by rule `r`."""
    then = quarter_back(period_end, 4)
    out = {"verdict": "unknown", "revenue": None, "revenue_prev": None, "profit": None,
           "profit_prev": None, "revenue_yoy_pct": None, "profit_yoy_pct": None,
           "surprise": None, "reasons": []}
    top = _pick(quarters, period_end, then, TOP_LINE)
    pat = _pick(quarters, period_end, then, PROFIT)
    if top:
        out["revenue"], out["revenue_prev"] = top[1], top[2]
    if pat:
        out["profit"], out["profit_prev"] = pat[1], pat[2]
    if not top or not pat or top[2] is None or pat[2] is None:
        out["reasons"].append("the quarter's revenue and profit, and the same quarter's a year "
                              "earlier, aren't all filed")
        return out
    if top[2] <= 0:
        out["reasons"].append("no revenue a year earlier to compare with")
        return out
    rev = 100 * (top[1] / top[2] - 1)
    out["revenue_yoy_pct"] = round(rev, 2)
    profit, prev = pat[1], pat[2]
    growth = 100 * (profit / prev - 1) if prev > 0 else None
    out["profit_yoy_pct"] = None if growth is None else round(growth, 2)
    s = surprise(quarters, period_end, pat[0])
    out["surprise"] = None if s is None else round(s, 2)
    min_rev, min_profit = r["min_revenue_growth_pct"], r["min_profit_growth_pct"]
    min_s = r["min_surprise"]
    turned = prev <= 0 < profit
    reasons = out["reasons"]
    reasons.append(f"revenue {rev:+.1f}% on a year earlier (positive needs {min_rev:+g}%)")
    if growth is not None:
        reasons.append(f"profit {growth:+.1f}% (positive needs {min_profit:+g}%)")
    elif turned:
        reasons.append(f"a profit of {_crore(profit)} after a loss of {_crore(-prev)} a "
                       "year earlier")
    else:
        reasons.append(f"a loss of {_crore(-profit)}, against a loss of {_crore(-prev)} a "
                       "year earlier")
    reasons.append(f"surprise {s:+.1f} (positive needs {min_s:+g})" if s is not None else
                   f"surprise not worked out: fewer than {MIN_SURPRISE_QUARTERS} earlier "
                   "quarters to compare with")
    narrowed = profit <= 0 and prev <= 0 and profit > prev
    if (profit <= 0 and not narrowed) or (prev > 0 and profit < prev) or (
            s is not None and s <= -min_s):
        out["verdict"] = "negative"
    elif (profit > 0 and rev >= min_rev and (turned or growth >= min_profit)
          and (s is None or s >= min_s)):
        out["verdict"] = "positive"
    else:
        out["verdict"] = "mixed"
    return out


def read(conn, company_id: int | None, period_end: dt.date, basis: str | None,
         filed_at: dt.datetime | None, r: dict) -> dict:
    """The read of a company's quarter on `basis` (without one: consolidated where filed,
    else standalone), from the figures filed by `filed_at`."""
    if company_id is None:
        return {**judge({}, period_end, r), "basis": basis}
    for b in ([basis] if basis else ["consolidated", "standalone"]):
        quarters = figures(conn, company_id, b, period_end, filed_at)
        if period_end in quarters or basis:
            return {**judge(quarters, period_end, r), "basis": b}
    return {**judge({}, period_end, r), "basis": None}


def parse_time(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    try:
        t = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else None


def parse_day(value) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])


def symbol(conn, company_id: int | None) -> str | None:
    if company_id is None:
        return None
    row = conn.execute("""select si.id_value from security s
                          join security_identifier si using (security_id)
                          where s.company_id = %s and si.id_type = 'NSE_SYMBOL'
                          order by si.valid_to is null desc, si.valid_from desc limit 1""",
                       (company_id,)).fetchone()
    return row[0] if row else None


def already_sent(conn, company_id: int | None, period_end: dt.date) -> bool:
    """A message for this company's quarter has gone (from another filing of it: the other
    basis, or the other exchange)."""
    return company_id is not None and conn.execute(
        "select exists (select 1 from results_read where company_id = %s "
        "and period_end = %s and sent)", (company_id, period_end)).fetchone()[0]


def keep(conn, notification_id: int, payload: dict, result: dict, sent: bool) -> None:
    conn.execute("""insert into results_read (notification_id, company_id, period_end, basis,
                        filed_at, verdict, revenue, revenue_prev, profit, profit_prev,
                        revenue_yoy_pct, profit_yoy_pct, surprise, reasons, sent)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (notification_id) do update set sent = excluded.sent,
                        read_at = now()""",
                 (notification_id, payload.get("company_id"), parse_day(payload["period_end"]),
                  result["basis"], parse_time(payload.get("filed_at")), result["verdict"],
                  result["revenue"], result["revenue_prev"], result["profit"],
                  result["profit_prev"], result["revenue_yoy_pct"], result["profit_yoy_pct"],
                  result["surprise"], "; ".join(result["reasons"]), sent))


def _signed(v: float | None, unit: str = "%") -> str:
    return "n/a" if v is None else f"{v:+.1f}{unit}"


def message(payload: dict, result: dict, sym: str | None, so_far: dict | None,
            below_high: float | None = None) -> str:
    """The Telegram message for a read report: the figures, the read, and the record; and
    for a positive read on a stock FALLEN_PCT or more below its one-year high, that the
    business may be turning near a bottom (docs/BOTTOMS.md)."""
    title = {"positive": "RESULTS READ POSITIVE"}.get(result["verdict"], "NEW QUARTERLY REPORT")
    name = str(payload.get("company") or "")[:250] + (f" ({sym})" if sym else "")
    lines = [title, name,
             f"Quarter ended {parse_day(payload['period_end']):%d %b %Y}, "
             f"{result['basis'] or payload.get('basis') or 'basis not specified'}"]
    if result["revenue"] is not None:
        lines.append(f"Revenue {_crore(result['revenue'])}, "
                     f"{_signed(result['revenue_yoy_pct'])} on the same quarter last year")
    if result["profit"] is not None:
        line = f"Profit {_crore(result['profit'])}"
        if result["profit_yoy_pct"] is not None:
            line += f", {_signed(result['profit_yoy_pct'])}"
        elif result["profit_prev"] is not None:
            line += f", against {_crore(result['profit_prev'])} a year earlier"
        lines.append(line)
    if result["surprise"] is not None:
        lines.append(f"Surprise {result['surprise']:+.1f} (the profit change on a year earlier "
                     "against its usual change, in standard deviations)")
    filed = parse_time(payload.get("filed_at"))
    lines.append(f"Filed on {payload.get('exchange') or 'the exchange'}"
                 + (f" at {filed.astimezone(IST):%H:%M IST, %d %b %Y}" if filed else ""))
    lines.append(f"Read {result['verdict']} by rule from the filed figures: "
                 + "; ".join(result["reasons"]) + ". Settings > Alerts sets the thresholds.")
    if result["verdict"] == "positive" and below_high is not None and below_high <= FALLEN_PCT:
        lines.append(f"The stock is {-below_high:.0f}% below its one-year high: if the "
                     "business is turning, this may be near a bottom. Unproven: no price "
                     "pattern called bottoms in the app's tests, and the Results days tab "
                     "(Paper tracking page) tracks whether positive results on fallen stocks do.")
    if so_far and so_far.get("n"):
        lines.append(f"Record so far: of the last {so_far['n']} positive reads with the next "
                     f"session traded, {so_far['up']} beat the Nifty 500 that session (average "
                     f"{so_far['mean']:+.1f}% against it). Not a recommendation.")
    else:
        lines.append("No record yet of how positive reads moved. Not a recommendation.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the record


def moves(conn, company_id: int, filed_at: dt.datetime) -> dict:
    """The stock's return and its excess over the Nifty 500 from the last close before the
    filing, to the first close after it and to 21 sessions on: {name: (return %, excess %)}.
    A horizon not traded yet is left out."""
    from igs.assistant.calls import (
        _index_series,
        _on_or_before,
        _price_rows,
        last_session,
        price_index,
    )
    base = last_session(filed_at)
    end = base + dt.timedelta(days=60)
    rows = _price_rows(conn, company_id, base - dt.timedelta(days=10), end)
    start = [i for i, r in enumerate(rows) if r[0] <= base]
    if not start:
        return {}
    rows = rows[start[-1]:]
    idx = price_index(rows)
    bench = _index_series(conn, base - dt.timedelta(days=10), end)
    b0 = _on_or_before(bench, rows[0][0])
    out = {}
    for name, n in MOVES.items():
        if len(idx) > n:
            day, level = idx[n]
            b1 = _on_or_before(bench, day)
            own = 100 * (level - 1)
            out[name] = (round(own, 2), round(own - 100 * (b1 / b0 - 1), 2)
                         if b0 and b1 else None)
    return out


def history(conn, limit: int = 200, verdict: str | None = None) -> list[dict]:
    """Each company's quarter once, newest first: the read that was sent, else the first, with
    its moves after the filing; only those read `verdict` if given."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            select * from (
                select distinct on (r.company_id, r.period_end) r.company_id, r.period_end,
                       r.basis, r.filed_at, r.verdict, r.revenue_yoy_pct::float8,
                       r.profit_yoy_pct::float8, r.surprise::float8, r.reasons, r.sent,
                       c.name company
                from results_read r left join company c using (company_id)
                where r.company_id is not null
                order by r.company_id, r.period_end, r.sent desc, r.read_at) x
            where %s::text is null or verdict = %s
            order by filed_at desc nulls last limit %s""", (verdict, verdict, limit))
        rows = cur.fetchall()
    for row in rows:
        row["symbol"] = symbol(conn, row["company_id"])
        row["moves"] = moves(conn, row["company_id"], row["filed_at"]) if row["filed_at"] \
            else {}
        row["below_high_pct"] = below_high(conn, row["company_id"], row["filed_at"]) \
            if row["filed_at"] else None
    return rows


def fallen(rows: list[dict]) -> list[dict]:
    """The positive reads on stocks FALLEN_PCT or more below their one-year high."""
    return [r for r in rows if r["verdict"] == "positive" and r.get("below_high_pct") is not None
            and r["below_high_pct"] <= FALLEN_PCT]


def below_high(conn, company_id: int, filed_at: dt.datetime) -> float | None:
    """How far, in %, the last close before the filing was below the year's highest close
    (adjusted for splits and bonuses); None without a year of prices."""
    from igs.assistant.calls import _price_rows, last_session, price_index
    base = last_session(filed_at)
    rows = _price_rows(conn, company_id, base - dt.timedelta(days=365), base)
    if len(rows) < 120:
        return None
    idx = price_index(rows)
    return round(100 * (idx[-1][1] / max(v for _, v in idx) - 1), 1)


def summary(rows: list[dict], horizon: str = "next_session") -> dict[str, dict]:
    """Per verdict: reads with the horizon traded (n), how many beat the Nifty 500 (up), and
    the average excess (mean)."""
    out = {}
    for verdict in VERDICTS:
        excess = [r["moves"][horizon][1] for r in rows if r["verdict"] == verdict
                  and horizon in r["moves"] and r["moves"][horizon][1] is not None]
        out[verdict] = {"n": len(excess), "up": sum(e > 0 for e in excess),
                        "mean": statistics.fmean(excess) if excess else None}
    return out


def positive_so_far(conn) -> dict:
    """The record the message quotes: the latest RECORD_READS positive reads and their next
    session."""
    return summary(history(conn, RECORD_READS, "positive"))["positive"]
