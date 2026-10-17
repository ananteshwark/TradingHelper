"""Paper portfolios of the 12-1 month momentum rule, tracked forward. Nothing here orders.

The rule, as backtested on 14 years of daily data (docs/MOMENTUM.md): at each month's last
close, rank the Nifty 200 members that traded at least Rs 50 crore a day over the previous
50 sessions by their return from 12 months to 1 month before (adjusted closes), and hold the
best ten, equally, from the next session's open until the next month's rebalance. A stock
still in the top ten is kept; one that drops out is sold.

Six tracks:
- 'rule': the ten best-ranked stocks.
- 'ranking': the ten the app's Rankings order puts first (the signal date's score run,
  Rejected stocks left out) among the same liquid members. Since 2026-10-17 that order is
  momentum: 6- and 12-month return over volatility (research/ml_rating), with red flags
  and the capped AI and sentiment overlays. This is its forward record.
- 'hedged': the ten best-ranked stocks that have futures (NSE's F&O lot list), as futures,
  with Nifty futures sold against them at the ten's average beta to the Nifty 50. Each
  month is a futures round trip on every position; capital not needed as margin earns 6%
  a year. Measured on the shares' and the Nifty's open-to-open moves, which futures track
  less their carry, which the hedge cancels. The most promising futures strategy in
  research/futures, not proven (docs/FUTURES_OPTIONS.md).
- 'risk': the same ten, each weighted by the inverse of its volatility over the previous 63
  sessions, so calmer stocks get more. In the 2013-2026 test of large managers' practices
  (docs/INSTITUTIONAL_ALGOS.md) this beat equal weights in both halves.
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

import numpy as np
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
VOL_DAYS = 63                              # sessions of daily returns for the risk weights
TRACKS = ("rule", "ai", "results", "risk", "hedged", "ranking")
NAMES = {"rule": "Rule", "ai": "AI-reviewed",
         "results": "Rule, skipping bad results", "risk": "Rule, risk-weighted",
         "hedged": "Hedged with Nifty futures", "ranking": "Rankings order"}
SHORT = {"rule": "rule", "ai": "AI-reviewed", "results": "skipping bad results",
         "risk": "risk-weighted", "hedged": "hedged", "ranking": "Rankings"}
STOCK_FUT_COST = 0.119      # % a round trip on a stock futures position (research/fno)
INDEX_FUT_COST = 0.079      # % a round trip on a Nifty futures position
CASH_YEAR = 0.06            # what capital not needed as margin earns
BETA_DAYS = 252
HEDGE_RANGE = (0.5, 1.5)


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


def ranking_picks(conn, run: dict | None, ranking: list[dict], top: int
                  ) -> tuple[list[dict], None, str]:
    """(holdings, None, note) for the 'ranking' track: the members the rule ranks, in the
    order of the signal date's score run (highest composite first, Rejected left out).
    Members the run did not score at all fill any places left, in the rule's order.
    Without a score run from on or before the signal date, the rule's picks."""
    if run is None:
        return ranking[:top], None, ("No score run from on or before the signal date: the "
                                     "rule's picks.")
    scored = conn.execute(
        """select company_id, tier <> 'Rejected' and composite is not null from score_result
           where run_id = %s order by composite desc nulls last, company_id""",
        (run["run_id"],)).fetchall()
    by_id = {r["company_id"]: r for r in ranking}
    kept = [by_id[c] for c, ranked in scored if ranked and c in by_id][:top]
    seen = {c for c, _ in scored}
    filled = [r for r in ranking if r["company_id"] not in seen][:top - len(kept)]
    note = f"In the order of score run {run['run_id']} ({run['as_of']:%d %b %Y})."
    if filled:
        note += (" Not scored by it, so in the rule's order: "
                 + ", ".join(r["symbol"] for r in filled) + ".")
    return kept + filled, None, note


def risk_picks(px: pl.DataFrame, signal: dt.date, ranking: list[dict], top: int
               ) -> tuple[list[dict], None, str]:
    """(holdings, None, note) for the 'risk' track: the rule's ten, each weighted by the
    inverse of its annualised volatility over the VOL_DAYS sessions to the signal date."""
    rule = ranking[:top]
    ids = [r["company_id"] for r in rule]
    hist = (px.filter(pl.col("company_id").is_in(ids) & (pl.col("trade_date") <= signal))
            .sort("company_id", "trade_date"))
    vol = {}
    for (cid,), g in hist.group_by("company_id"):
        rets = (g["adj_close"] / g["adj_close"].shift(1) - 1).drop_nulls().tail(VOL_DAYS)
        sd = rets.std()
        if rets.len() >= 20 and sd:
            vol[cid] = sd * math.sqrt(250)
    typical = sorted(vol.values())[len(vol) // 2] if vol else 1.0
    inv = {c: 1 / vol.get(c, typical) for c in ids}       # no history: the median's weight
    total = sum(inv.values())
    held = [{**r, "weight": round(inv[r["company_id"]] / total, 4),
             "vol_pct": round(vol[r["company_id"]] * 100, 1) if r["company_id"] in vol
             else None} for r in rule]
    calm, wild = max(held, key=lambda h: h["weight"]), min(held, key=lambda h: h["weight"])
    return held, None, (f"Weights by inverse {VOL_DAYS}-session volatility: from "
                        f"{calm['symbol']} {calm['weight']:.0%} to {wild['symbol']} "
                        f"{wild['weight']:.0%}.")


def futures_list(conn, day: dt.date) -> tuple[dict[str, int], int | None, dt.date | None]:
    """(lot size by stock symbol, the Nifty's lot size, the list's date) from NSE's F&O lot
    list on or before `day` (else the earliest), for the nearest contract month;
    ({}, None, None) when none is loaded."""
    as_of = conn.execute("""select coalesce(max(as_of) filter (where as_of <= %s), min(as_of))
                            from fno_lot""", (day,)).fetchone()[0]
    if as_of is None:
        return {}, None, None
    rows = conn.execute("""select distinct on (symbol, kind) symbol, kind, lot from fno_lot
                           where as_of = %s and month >= date_trunc('month', %s::date)
                           order by symbol, kind, month""", (as_of, day)).fetchall()
    return ({s: lot for s, k, lot in rows if k == "stock"},
            next((lot for s, k, lot in rows if s == "NIFTY" and k == "index"), None), as_of)


def hedged_picks(conn, px: pl.DataFrame, nifty: dict[dt.date, float], signal: dt.date,
                 ranking: list[dict], top: int) -> tuple[list[dict], None, str]:
    """(holdings, None, note) for the 'hedged' track: the best-ranked stocks with futures,
    each carrying the hedge ratio (the ten's average beta to the Nifty 50 over BETA_DAYS
    sessions, within HEDGE_RANGE), its lot size and price for the capital the note gives."""
    lots, nifty_lot, _ = futures_list(conn, signal)
    if lots:
        held = [r for r in ranking if r["symbol"] in lots][:top]
        cut = ranking.index(held[-1]) if len(held) == top else len(ranking)
        skipped = [r["symbol"] for r in ranking[:cut] if r["symbol"] not in lots]
    else:
        held, skipped = ranking[:top], []
    days = sorted(d for d in nifty if d <= signal)[-(BETA_DAYS + 1):]
    index = pl.DataFrame({"trade_date": days, "n": [nifty[d] for d in days]},
                         schema={"trade_date": pl.Date, "n": pl.Float64})
    hist = px.filter(pl.col("company_id").is_in([r["company_id"] for r in held])
                     & (pl.col("trade_date") <= signal))
    betas, last = [], {}
    for (cid,), g in hist.group_by("company_id"):
        g = g.sort("trade_date")
        last[cid] = g["adj_close"][-1]
        j = g.select("trade_date", "adj_close").join(index, on="trade_date").sort("trade_date")
        j = j.select((pl.col("adj_close") / pl.col("adj_close").shift(1) - 1).alias("s"),
                     (pl.col("n") / pl.col("n").shift(1) - 1).alias("m")).drop_nulls()
        if j.height >= 200 and j["m"].var():
            betas.append(float(np.cov(j["s"].to_numpy(), j["m"].to_numpy())[0, 1]
                               / j["m"].var()))
    hedge = float(min(max(np.mean(betas), HEDGE_RANGE[0]), HEDGE_RANGE[1])) if betas else 1.0
    holdings = [{**r, "weight": round(1 / len(held), 4), "hedge_beta": round(hedge, 3),
                 "lot": lots.get(r["symbol"]),
                 "price": round(last[r["company_id"]], 2) if r["company_id"] in last else None}
                for r in held]
    note = f"Short Nifty futures at {hedge:.2f} times the long value (the ten's average beta)."
    if skipped:
        note += f" Skipped without futures: {', '.join(skipped)}."
    if not lots:
        note += " No F&O lot list loaded yet, so stocks without futures may be included."
    long_value = sum(h["lot"] * h["price"] for h in holdings if h["lot"] and h["price"])
    n_close = nifty.get(signal)
    if long_value and nifty_lot and n_close:
        n_lots = max(1, round(hedge * long_value / (nifty_lot * n_close)))
        note += (f" To trade it with one lot of each: ₹{long_value / 1e5:,.1f} lakh of stock "
                 f"futures, hedged with {n_lots} Nifty lot{'s' if n_lots > 1 else ''} of "
                 f"₹{nifty_lot * n_close / 1e5:,.1f} lakh; margin is roughly 15–30% of that.")
    return holdings, None, note


def _index_return(close: dict[dt.date, float], open_: dict[dt.date, float], start: dt.date,
                  end: dt.date, complete: bool) -> float | None:
    """% move of the Nifty from `start`'s open to `end`'s open (complete) or close; a
    missing open is taken as the previous close."""
    def at_open(d):
        if open_.get(d):
            return open_[d]
        prev = _before(close, d)
        return close.get(prev) if prev else None
    a = at_open(start)
    b = at_open(end) if complete else close.get(end)
    return (b / a - 1) * 100 if a and b else None


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
            complete, nifty_open=None):
    ids = [h["company_id"] for h in holdings]
    rets = returns(px, ids, start, end, complete)
    if not rets:
        return None
    # Equal weights unless the holdings carry their own (the 'risk' track).
    weight = {h["company_id"]: h.get("weight", 1 / len(holdings)) for h in holdings}
    gross = sum(weight[c] * r for c, r in rets.items()) / sum(weight[c] for c in rets)
    entry = px.filter((pl.col("trade_date") == start) & pl.col("company_id").is_in(bought))
    costs = sum(weight[c] * cost_pct(p) for c, p in
                zip(entry["company_id"].to_list(), entry["adj_open"].to_list(), strict=True)
                if p)
    net = gross - costs
    hedge = holdings[0].get("hedge_beta") if holdings else None
    if hedge is not None:                  # 'hedged': futures, Nifty sold, cash on capital
        nret = _index_return(nifty, nifty_open or {}, start, end, complete)
        if nret is None:
            return None
        gross -= hedge * nret
        cash = ((1 + CASH_YEAR) ** ((end - start).days / 365) - 1) * 100
        net = gross - STOCK_FUT_COST - hedge * INDEX_FUT_COST + cash
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
         Jsonb([{"symbol": symbols[c], "return_pct": round(r, 2),
                 "weight_pct": round(weight[c] * 100, 1)} for c, r in rets.items()]
               + ([{"symbol": "NIFTY futures (short)", "return_pct": round(nret, 2),
                    "weight_pct": round(-hedge * 100, 1)}] if hedge is not None else []))))
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
    nifty_open = {d: float(o) for d, o in conn.execute(
        """select trade_date, open from index_price where index_name = %s and open is not null
           and trade_date between %s and %s""", (BENCHMARK, start, latest)).fetchall()}
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
                risk_picks(px, signal, ranking, top) if track == "risk" else
                hedged_picks(conn, px, nifty, signal, ranking, top) if track == "hedged" else
                ranking_picks(conn, _signal_run(conn, signal), ranking, top)
                if track == "ranking" else
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
                        prev_universe, complete=True, nifty_open=nifty_open)
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
                universe, complete=False, nifty_open=nifty_open)
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
        shown = [f"{h['symbol']} {h['weight']:.0%}" if "weight" in h and "hedge_beta" not in h
                 else h["symbol"] for h in holdings]
        lines.append(f"{names[track]}: holds {', '.join(shown)}"
                     + (f"; buys {', '.join(bought)}" if prev and bought else "")
                     + (f"; sells {', '.join(sold)}" if sold else "")
                     + (f"; short Nifty futures at {holdings[0]['hedge_beta']:.2f} times the "
                        "long value" if holdings and "hedge_beta" in holdings[0] else "") + ".")
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
    rule = res["rule"]["total"]
    starts = {t: res[t]["periods"][0]["start"] for t in TRACKS if res[t]["periods"]}
    if rule["net_pct"] is not None:
        parts = [f"{SHORT[t]}"
                 + (f" (from {starts[t]:%b %Y})" if t in starts
                    and starts[t] != starts.get("rule") else "")
                 + f" {_fmt(res[t]['total']['net_pct'])}" for t in TRACKS]
        lines.append(f"Since the start: {', '.join(parts)}, basket "
                     f"{_fmt(rule['basket_pct'])}, Nifty 50 {_fmt(rule['nifty_pct'])}.")
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
