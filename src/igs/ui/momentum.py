"""The momentum paper portfolios page (igs.momentum): holdings, the AI's reviews, and each
month's results against the equal-weighted Nifty 200 basket and the Nifty 50."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import momentum

NOTE = ("Paper only: nothing here places an order. Each month's last close ranks the Nifty "
        "200 stocks trading at least ₹50 crore a day by their return from 12 months to 1 month "
        "ago; the portfolios change at the next open. **Rule** holds the best ten. "
        "**AI-reviewed** holds the best ten the AI keeps after reading what the app holds on "
        "each, as of that close; it avoids a stock only for a specific reason it cites. **Rule, "
        "skipping bad results** holds the best ten not flagged by a results reaction of −5% "
        "or worse against the market in the previous 21 sessions (Results days page). **Rule, "
        "risk-weighted** holds the rule's ten, each weighted by the inverse of its volatility "
        "over the previous 63 sessions, so calmer stocks get more; in a 14-year test it beat "
        "equal weights in both halves (docs/INSTITUTIONAL_ALGOS.md). **Hedged with Nifty futures** "
        "holds the best ten that have futures, as futures, and sells Nifty futures against "
        "them at their average beta, so it earns how much they beat the market, plus 6% a "
        "year on the cash; its costs are a futures round trip on every position each month. "
        "It was the most promising futures strategy tested, not proven "
        "(docs/FUTURES_OPTIONS.md). **Rankings order** holds the ten of these stocks that "
        "the Rankings page puts first in that close's score run, red flags excluded; since "
        "17 Oct 2026 that order is momentum (6- and 12-month return over volatility, "
        "research/ml_rating), so this is its forward record. **Aladdin-style engine** spreads "
        "the same momentum score over all the stocks ranked with a factor risk model and an "
        "optimizer (at most 5% a stock, industries near their market weight), usually 30–60 "
        "stocks; in its 2016–2026 test it was smoother than the Rankings order but made less "
        "(research/aladdin_engine), and it is followed here at the owner's request. "
        "**Rankings order, untrimmed** holds the same ten as Rankings order but lets the "
        "winners run: a stock still among them keeps its grown weight instead of being reset "
        "to 10% each month (cut back only above 20%), and what is sold goes to the new names; "
        "in its 2016–2026 test this was 2 points a year ahead after tax but short of the bar "
        "(research/momentum_upgrades), and it is followed at the owner's request. Results "
        "are open to open between rebalances, after delivery costs (about 0.4% a round trip) "
        "on what was bought. The basket is the equal-weighted average of the stocks ranked. "
        "In the 14-year backtest the rule beat the basket in 2021–2026 and lagged it in "
        "2013–2020: this record shows how it does from here.")
TRACK_NAMES = momentum.NAMES
HISTORY_LABEL = "The Rankings order in history, 2016–2026"
# research/ai_tuning (results.txt): the top 10 and 20 by the momentum score among the 500
# most-traded stocks, held a month, after costs. The same data chose momentum.
HISTORY = """Before this forward record, on 2016–2026 NSE data (`research/ai_tuning`). These
are the stocks the Rankings page now puts first, held a month and replaced each month-end,
after costs (0.39% a round trip on the names replaced).

| | A year | After tax (rough) | Worst fall | Losing years |
|---|---|---|---|---|
| Top 10 | 27.0% | 21.3% | −46% | 4 of 10 |
| Top 20 | 28.1% | 22.3% | −39% | 3 of 10 |
| 500 most traded, equal weights | 13.1% | | −55% | 4 of 10 |
| Nifty 50 (price) | 10.2% | | −29% | 0 of 10 |

Top 20 by year: 2016 −17%, 2017 +114%, 2018 −17%, 2019 0%, 2020 +57%, 2021 +87%,
2022 +5%, 2023 +53%, 2024 +48%, 2025 +4%, 2026 to September +32%.

- **The worst fall** ran from August 2018 to March 2020.
- **The data:** these are the same data that chose momentum, so the figures flatter it.
  Edges also shrink once they are widely known.
- **What's left out:** dividends, and falls inside a month.
- **Letting the past set the weights:** four procedures tried it (factor momentum, a
  walk-forward optimiser, learned weights, a momentum-window tuner). Each did worse than
  momentum alone, so the ranking keeps fixed weights."""


def _pct(v):
    return None if v is None else round(v, 2)


def page(conn) -> None:
    st.header("Momentum paper portfolios")
    st.caption(NOTE)
    with st.expander(HISTORY_LABEL):
        st.markdown(HISTORY)
    hold, res = momentum.holdings(conn), momentum.results(conn)
    if not hold["rule"]:
        st.info("No portfolio yet. The daily job forms the first one once the Nifty 200 list "
                "(loaded by the check each day) and a year of prices are in.")
        return
    totals = {t: res[t]["total"] for t in momentum.TRACKS}
    first = {t: res[t]["periods"][0]["start"] for t in momentum.TRACKS if res[t]["periods"]}

    def later(t):
        return (f"Since the {first[t]:%d %b %Y} open; it started later than the rule."
                if t in first and first[t] != first.get("rule") else None)
    labels = {"rule": "Rule, since the start", "ai": "AI-reviewed, since the start",
              "results": "Skipping bad results", "risk": "Risk-weighted", "hedged": "Hedged",
              "ranking": "Rankings order", "engine": "Aladdin-style engine",
              "untrimmed": "Untrimmed"}
    m = st.columns(len(momentum.TRACKS) + 2)
    for col, track in zip(m, momentum.TRACKS, strict=False):
        col.metric(labels[track], _fmt(totals[track]["net_pct"]), help=later(track))
    m[-2].metric("Nifty 200 basket", _fmt(totals["rule"]["basket_pct"]))
    m[-1].metric("Nifty 50", _fmt(totals["rule"]["nifty_pct"]))
    for tab, track in zip(st.tabs([TRACK_NAMES[t] for t in momentum.TRACKS]), momentum.TRACKS,
                          strict=True):
        with tab:
            _track(hold[track], res[track])


def _track(h: dict | None, r: dict) -> None:
    if h is None:
        st.info("Not formed yet: it starts at the next monthly rebalance.")
        return
    st.caption(f"Formed from the {h['signal_date']:%d %b %Y} close of {h['universe']} ranked "
               f"stocks; holdings changed at the {h['entry_date']:%d %b %Y} open. "
               + (h["note"] or ""))
    open_period = next((p for p in r["periods"] if not p["complete"]), None)
    since = {d["symbol"]: d["return_pct"] for d in (open_period or {}).get("detail", [])}
    st.dataframe(pl.DataFrame([{"rank": x["rank"], "stock": x["symbol"],
                                "12-1 month return %": x["score_pct"],
                                **({"weight %": round(x["weight"] * 100, 1),
                                    "volatility %": x.get("vol_pct")}
                                   if "weight" in x and "hedge_beta" not in x else {}),
                                **({"lot": x.get("lot"),
                                    "one lot ₹ lakh": round(x["lot"] * x["price"] / 1e5, 2)
                                    if x.get("lot") and x.get("price") else None}
                                   if "hedge_beta" in x else {}),
                                "since entry %": since.get(x["symbol"])}
                               for x in h["holdings"]]), hide_index=True, width="stretch")
    if h["reviews"]:
        st.subheader("The AI's reviews")
        st.dataframe(pl.DataFrame([{"rank": x["rank"], "stock": x["symbol"],
                                    "decision": x["decision"], "reason": x["reason"]}
                                   for x in h["reviews"]]), hide_index=True, width="stretch")
    if r["periods"]:
        st.subheader("Each month")
        st.dataframe(pl.DataFrame([{
            "from": p["start"], "to": p["end"],
            "status": "closed" if p["complete"] else "open (to the latest close)",
            "after costs %": _pct(p["net_pct"]), "basket %": _pct(p["basket_pct"]),
            "Nifty 50 %": _pct(p["nifty_pct"]),
            "vs basket %": (None if p["basket_pct"] is None
                            else round(p["net_pct"] - p["basket_pct"], 2)),
            "bought": p["bought"]} for p in reversed(r["periods"])]),
            hide_index=True, width="stretch")


def _fmt(v) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"
