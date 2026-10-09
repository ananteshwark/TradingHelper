"""Paper portfolios of the 12-1 month momentum rule, tracked forward. Nothing here orders.

The rule, as backtested on 14 years of daily data (docs/MOMENTUM.md): at each month's last
close, rank the Nifty 200 members that traded at least Rs 50 crore a day over the previous
50 sessions by their return from 12 months to 1 month before (adjusted closes), and hold the
best ten, equally, from the next session's open until the next month's rebalance. A stock
still in the top ten is kept; one that drops out is sold.

Three tracks:
- 'rule': the ten best-ranked stocks.
- 'results': the ten best-ranked stocks not flagged by a bad results reaction (a -5% or
  worse move against the market around results, in the 21 sessions before the signal date;
  igs.results_drift). The next-ranked unflagged stock takes a flagged one's place.
- 'ai': the ten best-ranked stocks the AI's review keeps. The AI reads what the app holds
  on each stock as of the signal date's score run, and may avoid one only for a specific,
  documented reason; the next-ranked stock it keeps takes the place. Without a score run
  from on or before the signal date (its evidence must not postdate the entry) or an
  available assistant, the track holds the rule's picks and says so.

Each holding period runs open to open between rebalances, after delivery costs (STT both
sides, stamp, exchange, SEBI, GST, Rs 20 brokerage an order, DP charge, 0.05% slippage each
side) on the names bought at its start. It is compared with the equal-weighted average of
the stocks ranked (the basket, no costs) and the Nifty 50. The current period is marked to
the latest close.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import polars as pl
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from igs.timeutil import IST, end_of_day_ist

TOP = 10
LOOKBACK, SKIP = 252, 21                   # trading days: 12 months, skipping the last one
MIN_TURNOVER = 50e7                        # Rs 50 crore a day
TURNOVER_DAYS = 50
INDEX = "NIFTY 200"
BENCHMARK = "Nifty 50"
REVIEW_MAX = 20                            # candidates the AI reviews at most a month
TRACKS = ("rule", "ai", "results")
NAMES = {"rule": "Rule", "ai": "AI-reviewed",
         "results": "Rule, skipping bad results"}


def cost_pct(price: float) -> float:
    """Round-trip delivery costs on Rs 1 lakh at `price`, % of the amount bought."""
    qty = math.floor(1e5 / price)
    if qty < 1:
        return 0.0
    value = qty * price
    turnover = 2 * value
    exch, sebi, brokerage = 0.0000297 * turnover, 0.000001 * turnover, 40.0
    total = (0.001 * turnover + 0.00015 * value + exch + sebi + brokerage
             + 0.18 * (brokerage + exch + sebi) + 15.93 + 0.0005 * turnover)
    return total / value * 100


# --------------------------------------------------------------------------- prices


def panel(view) -> pl.DataFrame:
    """Each company's primary line: trade_date, adjusted open and close, traded value."""
    from igs.factors.base import primary_prices
    px = primary_prices(view)
    return px.select("company_id", "symbol", "trade_date", "adj_open", "adj_close",
                     pl.col("turnover_inr").fill_null(pl.col("close") * pl.col("volume"))
                     .alias("turnover")).sort("company_id", "trade_date")


def trading_days(px: pl.DataFrame, min_names: int = 20) -> list[dt.date]:
    counts = px.group_by("trade_date").len()
    return sorted(counts.filter(pl.col("len") >= min(min_names, counts["len"].max() or 0))
                  ["trade_date"].to_list())


def month_starts(days: list[dt.date]) -> list[dt.date]:
    """The first trading day of each month after the first month in `days`."""
    return [d for prev, d in zip(days, days[1:], strict=False)
            if (d.year, d.month) != (prev.year, prev.month)]


def rank(px: pl.DataFrame, members: set[int], signal: dt.date, *,
         min_turnover: float = MIN_TURNOVER) -> list[dict]:
    """Liquid members with a full year of prices up to `signal`, best 12-1 return first:
    [{company_id, symbol, score_pct, rank}]."""
    hist = px.filter(pl.col("company_id").is_in(list(members))
                     & (pl.col("trade_date") <= signal))
    out = []
    for (cid,), g in hist.group_by("company_id"):
        g = g.sort("trade_date")
        if g.height < LOOKBACK + 1 or g["trade_date"][-1] != signal:
            continue
        if g["turnover"].tail(TURNOVER_DAYS).mean() < min_turnover:
            continue
        close = g["adj_close"]
        then, recent = close[-1 - LOOKBACK], close[-1 - SKIP]
        if then and recent:
            out.append({"company_id": cid, "symbol": g["symbol"][-1],
                        "score_pct": round((recent / then - 1) * 100, 2)})
    out.sort(key=lambda r: (-r["score_pct"], r["company_id"]))
    return [{**r, "rank": i} for i, r in enumerate(out, 1)]


def returns(px: pl.DataFrame, ids: list[int], start: dt.date, end: dt.date,
            complete: bool) -> dict[int, float]:
    """% return of each company from `start`'s open to `end`'s open (complete) or close."""
    col = "adj_open" if complete else "adj_close"
    a = px.filter((pl.col("trade_date") == start) & pl.col("company_id").is_in(ids))
    b = px.filter((pl.col("trade_date") == end) & pl.col("company_id").is_in(ids))
    first = dict(zip(a["company_id"], a["adj_open"], strict=True))
    last = dict(zip(b["company_id"], b[col], strict=True))
    return {c: (last[c] / first[c] - 1) * 100 for c in ids
            if first.get(c) and last.get(c)}


# --------------------------------------------------------------------------- AI review


class Review(BaseModel):
    decision: str = Field(description="keep or avoid", pattern="^(keep|avoid)$")
    reason: str = Field(description="One or two sentences citing the data relied on.")


SYSTEM = """\
You review one stock for a paper portfolio built by a price momentum rule: it holds, for one
month, the Nifty 200 stocks with the strongest return from 12 months to 1 month ago.
Momentum has worked on average, so the default is to keep the stock. Answer "avoid" only
for a specific, documented reason in the data given that makes a fall over the next month
likely: for example regulatory, legal or forensic action; accounting red flags; promoters
pledging or selling heavily; results or guidance sharply worse than before; a corporate
event (demerger, delisting, open offer, merger swap) that changes what the price means; or
a recent jump caused by a one-off event that has already played out. High valuation, a big
past rise or general market worries are not reasons: the rule buys exactly those. Use only
the data given, as of its date. Ignore anything you may know about the company or market
after that date. The reason must cite what in the data it rests on."""


def review(assistant, conn, run: dict, candidate: dict, universe: int) -> dict:
    """The AI's keep / avoid on one candidate: {symbol, rank, decision, reason}."""
    from igs.assistant.calls import gather, schema_of
    from igs.service import NotFound
    try:
        _, _, data = gather(conn, run, candidate["symbol"])
    except NotFound:
        return {"symbol": candidate["symbol"], "rank": candidate["rank"],
                "decision": "keep", "reason": "Not in the score run: nothing to review."}
    as_of = run["as_of"].astimezone(IST)
    prompt = (f"As of {as_of:%d %b %Y}. {candidate['symbol']} ranks {candidate['rank']} of "
              f"{universe} by 12-1 month return ({candidate['score_pct']:+.1f}%).\n"
              "Everything the app holds on the stock, as of that date (JSON):\n"
              + json.dumps(data, default=str))
    parsed, _ = assistant.structured("momentum", system=SYSTEM, prompt=prompt,
                                     schema=schema_of(Review))
    r = Review.model_validate(parsed)
    return {"symbol": candidate["symbol"], "rank": candidate["rank"],
            "decision": r.decision, "reason": r.reason[:600]}


def ai_picks(assistant, conn, run: dict | None, ranking: list[dict], top: int
             ) -> tuple[list[dict], list[dict] | None, str]:
    """(holdings, reviews, note) for the 'ai' track."""
    rule = ranking[:top]
    if assistant is None:
        return rule, None, "No AI review (assistant off): the rule's picks."
    if run is None:
        return rule, None, ("No AI review: no score run from on or before the signal date, "
                            "so the evidence would postdate the entry. The rule's picks.")
    from igs.assistant.errors import AssistantError, AssistantUnavailable
    kept, reviews, note = [], [], ""
    for cand in ranking[:REVIEW_MAX]:
        if len(kept) == top:
            break
        try:
            r = review(assistant, conn, run, cand, len(ranking))
        except (AssistantUnavailable, AssistantError) as exc:   # budget, provider, answer
            note = (f"AI review stopped after {len(reviews)} ({type(exc).__name__}); the rest "
                    "are the next-ranked stocks, unreviewed.")
            break
        reviews.append(r)
        if r["decision"] == "keep":
            kept.append(cand)
    for cand in ranking:
        if len(kept) == top:
            break
        if cand not in kept and not any(r["symbol"] == cand["symbol"]
                                        and r["decision"] == "avoid" for r in reviews):
            kept.append(cand)
    return kept, reviews, note


def results_picks(conn, signal: dt.date, ranking: list[dict], top: int
                  ) -> tuple[list[dict], None, str]:
    """(holdings, None, note) for the 'results' track: the rule's ranking without the stocks
    flagged by a bad results reaction known by the signal date's close."""
    from igs.results_drift import flagged
    flags = flagged(conn, end_of_day_ist(signal))
    kept = [r for r in ranking if r["company_id"] not in flags][:top]
    cut = ranking.index(kept[-1]) if len(kept) == top else len(ranking)
    skipped = [f"{r['symbol']} ({flags[r['company_id']]['abnormal_pct']:+.1f}% on results)"
               for r in ranking[:cut] if r["company_id"] in flags]
    return kept, None, ("Skipped after bad results: " + ", ".join(skipped) + "."
                        if skipped else "No stock in the top ten was flagged.")


# --------------------------------------------------------------------------- database


def members(conn, day: dt.date) -> tuple[set[int], dt.date | None]:
    """The index's members at `day` (the latest snapshot on or before it, else the earliest
    snapshot, with its date so the record can say so)."""
    row = conn.execute("""select coalesce(max(as_of) filter (where as_of <= %s), min(as_of))
                          from index_member where index_name = %s""", (day, INDEX)).fetchone()
    if row[0] is None:
        return set(), None
    ids = conn.execute("select company_id from index_member where index_name = %s "
                       "and as_of = %s", (INDEX, row[0])).fetchall()
    return {r[0] for r in ids}, row[0]


def _latest(conn, track: str) -> dict | None:
    row = conn.execute("""select signal_date, entry_date, holdings from momentum_rebalance
                          where track = %s order by signal_date desc limit 1""",
                       (track,)).fetchone()
    return dict(zip(("signal_date", "entry_date", "holdings"), row, strict=True)) if row else None


def _signal_run(conn, signal: dt.date) -> dict | None:
    from igs.service import resolve_run
    row = conn.execute("""select run_id from score_run where as_of <= %s
                          order by as_of desc, run_id desc limit 1""",
                       (end_of_day_ist(signal),)).fetchone()
    return resolve_run(conn, row[0]) if row else None


def _period(conn, px, nifty, track, start, end, signal, holdings, bought, universe_ids,
            complete):
    ids = [h["company_id"] for h in holdings]
    rets = returns(px, ids, start, end, complete)
    if not rets:
        return None
    gross = sum(rets.values()) / len(rets)
    entry = px.filter((pl.col("trade_date") == start) & pl.col("company_id").is_in(bought))
    costs = sum(cost_pct(p) for p in entry["adj_open"].to_list() if p)
    net = gross - costs / len(ids)
    basket = returns(px, universe_ids, start, end, complete)
    n0, n1 = nifty.get(signal), nifty.get(end if not complete else _before(nifty, end))
    symbols = {h["company_id"]: h["symbol"] for h in holdings}
    conn.execute("""insert into momentum_period (track, start_date, end_date, complete,
            portfolio_gross_pct, portfolio_net_pct, basket_pct, nifty_pct, bought, detail)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (track, start_date) do update set end_date = excluded.end_date,
            complete = excluded.complete, portfolio_gross_pct = excluded.portfolio_gross_pct,
            portfolio_net_pct = excluded.portfolio_net_pct, basket_pct = excluded.basket_pct,
            nifty_pct = excluded.nifty_pct, bought = excluded.bought, detail = excluded.detail,
            updated_at = now()""",
        (track, start, end, complete, round(gross, 3), round(net, 3),
         round(sum(basket.values()) / len(basket), 3) if basket else None,
         round((n1 / n0 - 1) * 100, 3) if n0 and n1 else None, len(bought),
         Jsonb([{"symbol": symbols[c], "return_pct": round(r, 2)} for c, r in rets.items()])))
    return {"gross": gross, "net": net}


def _before(series: dict[dt.date, float], day: dt.date) -> dt.date | None:
    prior = [d for d in series if d < day]
    return max(prior) if prior else None


def run(conn, *, assistant=None, top: int = TOP, min_turnover: float = MIN_TURNOVER,
        notify=None) -> str:
    """Rebalance at each month start not yet recorded (from the latest one on the first run),
    close the period it ends, mark the open one to the latest close, and send a note for a
    rebalance. Returns a one-line summary."""
    from igs.factors.base import index_closes
    from igs.pit.loader import load_dataset
    from igs.pit.view import PitView
    latest = conn.execute("select max(trade_date) from price_eod where exchange = 'NSE'"
                          ).fetchone()[0]
    if latest is None:
        return "no prices loaded"
    first_open = conn.execute("select min(start_date) from momentum_period where not complete"
                              ).fetchone()[0]
    start = min(latest - dt.timedelta(days=420),
                (first_open or latest) - dt.timedelta(days=420))
    view = PitView(load_dataset(conn, start, latest), end_of_day_ist(latest))
    px = panel(view)
    nifty = dict(index_closes(view, BENCHMARK).iter_rows())
    days = trading_days(px)
    starts = month_starts(days)
    if not starts:
        return "not enough price history"
    done = {t: _latest(conn, t) for t in TRACKS}
    last_entry = min((d["entry_date"] for d in done.values() if d), default=None)
    due = [e for e in starts if last_entry is None or e > last_entry]
    if last_entry is None:
        due = due[-1:]                       # first run: start from the latest month start
    made = []
    for entry in due:
        signal = days[days.index(entry) - 1]
        ids, as_of = members(conn, signal)
        if not ids:
            return "no Nifty 200 members loaded yet (the check loads the list daily)"
        ranking = rank(px, ids, signal, min_turnover=min_turnover)
        if len(ranking) < top:
            return f"only {len(ranking)} liquid Nifty 200 members with a year of prices"
        note = (f"Members as listed on {as_of:%d %b %Y}." if as_of and as_of > signal else "")
        for track in TRACKS:
            prev = _latest(conn, track)
            if prev and prev["entry_date"] >= entry:
                continue
            holdings, reviews, why = (
                (ranking[:top], None, "") if track == "rule" else
                results_picks(conn, signal, ranking, top) if track == "results" else
                ai_picks(assistant, conn, _signal_run(conn, signal), ranking, top))
            if prev:
                prev_ids, _ = members(conn, prev["signal_date"])
                prev_universe = [r["company_id"] for r in
                                 rank(px, prev_ids, prev["signal_date"],
                                      min_turnover=min_turnover)]
                held_before = _held_before(conn, track, prev["signal_date"])
                _period(conn, px, nifty, track, prev["entry_date"], entry,
                        prev["signal_date"], prev["holdings"],
                        [h["company_id"] for h in prev["holdings"]
                         if h["company_id"] not in held_before],
                        prev_universe, complete=True)
            conn.execute("""insert into momentum_rebalance (track, signal_date, entry_date,
                    universe, holdings, reviews, note) values (%s, %s, %s, %s, %s, %s, %s)""",
                         (track, signal, entry, len(ranking), Jsonb(holdings),
                          Jsonb(reviews) if reviews is not None else None,
                          " ".join(x for x in (note, why) if x)))
            made.append((track, signal, entry, holdings, reviews, prev))
        conn.commit()
    # Mark each track's open period to the latest close.
    for track in TRACKS:
        cur = _latest(conn, track)
        if cur is None or cur["entry_date"] > latest:
            continue
        held_before = _held_before(conn, track, cur["signal_date"])
        ids, _ = members(conn, cur["signal_date"])
        universe = [r["company_id"] for r in rank(px, ids, cur["signal_date"],
                                                   min_turnover=min_turnover)]
        _period(conn, px, nifty, track, cur["entry_date"], latest, cur["signal_date"],
                cur["holdings"], [h["company_id"] for h in cur["holdings"]
                                  if h["company_id"] not in held_before],
                universe, complete=False)
    conn.commit()
    if made and notify is not None:
        notify(message(conn, made))
    return (f"rebalanced {len({m[2] for m in made})} month(s); marked to {latest:%d %b %Y}"
            if made else f"marked to {latest:%d %b %Y}")


def _held_before(conn, track: str, signal: dt.date) -> set[int]:
    """The companies the track held before its rebalance at `signal`."""
    row = conn.execute("""select holdings from momentum_rebalance where track = %s
                          and signal_date < %s order by signal_date desc limit 1""",
                       (track, signal)).fetchone()
    return {h["company_id"] for h in row[0]} if row else set()


# --------------------------------------------------------------------------- reporting


def results(conn) -> dict:
    """Per track: periods (oldest first) and compounded totals since the start."""
    out = {}
    for track in TRACKS:
        rows = conn.execute("""select start_date, end_date, complete,
                portfolio_gross_pct::float8, portfolio_net_pct::float8, basket_pct::float8,
                nifty_pct::float8, bought, detail from momentum_period where track = %s
                order by start_date""", (track,)).fetchall()
        cols = ("start", "end", "complete", "gross_pct", "net_pct", "basket_pct",
                "nifty_pct", "bought", "detail")
        periods = [dict(zip(cols, r, strict=True)) for r in rows]

        def total(key, periods=periods):
            vals = [p[key] for p in periods if p[key] is not None]
            return round((math.prod(1 + v / 100 for v in vals) - 1) * 100, 2) if vals else None
        out[track] = {"periods": periods,
                      "total": {k: total(k) for k in ("net_pct", "basket_pct", "nifty_pct")}}
    return out


def holdings(conn) -> dict:
    """Per track: the latest rebalance (holdings, reviews, note)."""
    out = {}
    for track in TRACKS:
        row = conn.execute("""select signal_date, entry_date, universe, holdings, reviews, note
                              from momentum_rebalance where track = %s
                              order by signal_date desc limit 1""", (track,)).fetchone()
        out[track] = (dict(zip(("signal_date", "entry_date", "universe", "holdings",
                                "reviews", "note"), row, strict=True)) if row else None)
    return out


def message(conn, made: list[tuple]) -> str:
    """The Telegram note for the rebalances just made."""
    entry = made[-1][2]
    lines = [f"MOMENTUM PAPER PORTFOLIOS · {entry:%b %Y} (paper only; no orders)",
             f"From the {made[-1][1]:%d %b} close; holdings change at the {entry:%d %b} open."]
    names = NAMES
    for track, _signal, made_entry, holdings, reviews, prev in made:
        if made_entry != entry:                 # describe the latest month's rebalance
            continue
        before = {h["symbol"] for h in prev["holdings"]} if prev else set()
        now = [h["symbol"] for h in holdings]
        bought = [s for s in now if s not in before]
        sold = sorted(before - set(now))
        lines.append(f"{names[track]}: holds {', '.join(now)}"
                     + (f"; buys {', '.join(bought)}" if prev and bought else "")
                     + (f"; sells {', '.join(sold)}" if sold else "") + ".")
        for r in reviews or []:
            if r["decision"] == "avoid":
                lines.append(f"  AI avoided {r['symbol']} (rank {r['rank']}): {r['reason']}")
    res = results(conn)
    for track in TRACKS:
        done = [p for p in res[track]["periods"] if p["complete"]]
        if done:
            p = done[-1]
            lines.append(f"{names[track]}, {p['start']:%d %b}–{p['end']:%d %b}: "
                         f"{p['net_pct']:+.2f}% after costs; basket "
                         f"{_fmt(p['basket_pct'])}, Nifty 50 {_fmt(p['nifty_pct'])}.")
    rule, ai, skip = (res[t]["total"] for t in TRACKS)
    starts = {t: res[t]["periods"][0]["start"] for t in TRACKS if res[t]["periods"]}
    later = (f" (from {starts['results']:%b %Y})" if "results" in starts
             and starts["results"] != starts.get("rule") else "")
    if rule["net_pct"] is not None:
        lines.append(f"Since the start: rule {_fmt(rule['net_pct'])}, AI-reviewed "
                     f"{_fmt(ai['net_pct'])}, skipping bad results{later} "
                     f"{_fmt(skip['net_pct'])}, basket {_fmt(rule['basket_pct'])}, Nifty 50 "
                     f"{_fmt(rule['nifty_pct'])}.")
    lines.append("https://stocks.ednis.ai/")
    return "\n".join(lines)


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def step(conn) -> str:
    """The daily job's step: the assistant when it is on, Telegram for the note."""
    from igs.alerts.delivery import send_telegram
    from igs.assistant.llm import Assistant, AssistantUnavailable
    try:
        assistant = Assistant.open(conn)
    except AssistantUnavailable:
        assistant = None
    return run(conn, assistant=assistant, notify=send_telegram)
