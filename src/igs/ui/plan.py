"""The Plan page (igs.plan): the paper portfolio of momentum stocks, gold and cash in rupees,
the orders for the next open, every rebalance, and money added or withdrawn."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import plan
from igs.config import load_plan

NOTE = ("Paper only: nothing here places an order. The plan keeps {core:g}% in momentum "
        "stocks, {gold:g}% in gold ({gold_symbol}) and {cash:g}% in cash earning "
        "{cash_return:g}% a year. Each month's last close picks the {stocks} best-ranked "
        "Nifty 200 momentum stocks, leaving out any with a bad results reaction or a "
        "serious red flag, weighted towards the calmer ones and none above {max_stock:g}% "
        "of the part. All three parts go back to their shares each quarter, and whenever "
        "gold or cash drifts more than {drift:g} points. The orders for the next open are "
        "whole shares worked out at the close and sent on Telegram; this record fills them "
        "at the open after charges. On 2013–2026 data this mix made about 20% a year before "
        "tax and 17% after, with a worst fall of 30% (illustrative, not a pre-registered "
        "test; docs/PLAN.md). Expect less from here, and falls of that size.")
SIDE = {"buy": "BUY", "sell": "SELL"}


def page(conn) -> None:
    cfg = load_plan()
    st.header("The Plan (paper)")
    st.caption(NOTE.format(core=cfg.core_pct, gold=cfg.gold_pct, cash=cfg.cash_pct,
                           gold_symbol=cfg.gold_symbol, cash_return=cfg.cash_return_pct,
                           stocks=cfg.stocks, max_stock=cfg.max_stock_pct,
                           drift=cfg.drift_points))
    s = plan.summary(conn)
    if s is None:
        st.info(f"Not started yet. The daily job starts it with {plan.inr(cfg.capital_inr)} "
                "(config/plan.yaml) once prices, the Nifty 200 list and gold prices are in, "
                "and sends the first orders on Telegram.")
        return
    m = st.columns(5)
    m[0].metric("Value", plan.inr(s["value"]),
                help=(f"Including {plan.inr(s['added_inr'])} added or withdrawn"
                      if s["added_inr"] else None))
    m[1].metric(f"Since {s['start']:%d %b %Y}", f"{s['since_pct']:+.2f}%",
                help="After charges; money added or withdrawn doesn't count.")
    m[2].metric("Nifty 50, same period", "n/a" if s["nifty_pct"] is None
                else f"{s['nifty_pct']:+.2f}%")
    m[3].metric("Stocks / gold / cash", " / ".join(
        "n/a" if v is None else f"{v:.0f}%" for v in s["shares"].values()))
    m[4].metric("Marked to", f"{s['trade_date']:%d %b %Y}")
    rebs = plan.rebalances(conn)
    pending = [r for r in rebs if r["filled_on"] is None]
    if pending:
        r = pending[0]
        st.subheader(f"Orders for the {r['session_date']:%d %b %Y} open")
        st.caption(plan.REASONS[r["reason"]].format(core=cfg.core_pct, gold=cfg.gold_pct,
                                                    cash=cfg.cash_pct, drift=cfg.drift_points)
                   + (" " + r["note"] if r["note"] else ""))
        _orders(r["orders"], filled=False)
    st.subheader("Holdings")
    rows = [{"part": "stocks" if h["kind"] == "stock" else "gold", "symbol": h["symbol"],
             "shares": h["shares"], "close ₹": h["close"], "value ₹": round(h["value_inr"]),
             "share of plan %": round(h["value_inr"] / s["value"] * 100, 1)}
            for h in sorted(s["holdings"], key=lambda h: (h["kind"], -h["value_inr"]))]
    rows.append({"part": "cash", "symbol": "", "shares": None, "close ₹": None,
                 "value ₹": round(s["cash"]),
                 "share of plan %": round(s["cash"] / s["value"] * 100, 1)})
    st.dataframe(pl.DataFrame(rows), hide_index=True, width="stretch")
    hist = plan.history(conn)
    first_nifty = next((h["nifty_close"] for h in hist if h["nifty_close"]), None)
    if len(hist) > 1:
        st.subheader("Value of ₹100 since the start")
        st.line_chart(pl.DataFrame([
            {"date": h["trade_date"], "The Plan": round(h["unit_value"], 2),
             "Nifty 50": (round(h["nifty_close"] / first_nifty * 100, 2)
                          if h["nifty_close"] and first_nifty else None)}
            for h in hist]).to_pandas().set_index("date"))
    done = [r for r in rebs if r["filled_on"] is not None]
    if done:
        st.subheader("Rebalances")
        for r in done:
            with st.expander(f"{r['session_date']:%d %b %Y} · {r['reason']} · "
                             f"{len(r['orders'])} orders"):
                if r["note"]:
                    st.caption(r["note"])
                _orders(r["orders"], filled=True)
    _flows(conn)


def _orders(orders: list[dict], filled: bool) -> None:
    if not orders:
        st.write("No orders: the holdings were close enough to their targets.")
        return
    st.dataframe(pl.DataFrame([{
        "order": SIDE[o["side"]], "symbol": o["symbol"], "shares": o["shares"],
        "at about ₹": o["price"], "value ₹": round(o["shares"] * (o["price"] or 0)),
        "why": o["why"],
        **({"filled": o.get("filled"), "open ₹": o.get("fill_price"),
            "charges ₹": o.get("charges_inr"), "note": o.get("fill_note", "")}
           if filled else {})}
        for o in sorted(orders, key=lambda o: (o["side"] != "sell", o["symbol"]))]),
        hide_index=True, width="stretch")


def _flows(conn) -> None:
    from igs.ui import auth
    if not auth.is_admin():
        return
    with st.expander("Add or withdraw money"):
        st.caption("Applied at the next session's open. Adding money usually lifts cash above "
                   "its share, so the next orders invest it across the three parts.")
        with st.form("plan_flow"):
            amount = st.number_input("Amount ₹ (negative to withdraw)", step=10000.0,
                                     value=0.0)
            note = st.text_input("Note", value="")
            if st.form_submit_button("Record") and amount:
                plan.add_flow(conn, amount, note)
                st.success(f"Recorded {plan.inr(amount)}; it applies at the next open.")
