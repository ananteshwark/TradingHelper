"""The ML intraday calls page (igs.intraday.ml): today's calls, real orders for them when
switched on, each day's paper result and the record since the start."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs.intraday import ml
from igs.intraday.costs import intraday_rates
from igs.intraday.trading import (
    ML_APPROVE_UNTIL,
    ML_MIN_USEFUL_RUPEES,
    ML_SQUARE_OFF,
    TradeError,
    approve_ml,
    ml_preview,
    ml_window,
    trading_token,
)
from igs.intraday.trading import settings as trade_settings
from igs.timeutil import IST, utc_now
from igs.ui import auth

NOTE = ("The record below is on paper. At 09:45 a machine-learning model predicts "
        "each liquid Nifty 200 stock's move to 15:15 from its first 30 minutes, its recent "
        "days and the market's. The three highest predictions above +0.15% are buys and the "
        "three lowest below −0.15% short sells. Each enters at the next 5-minute candle's open "
        "and exits at the 15:15 candle's open; results are after slippage and intraday charges "
        "on ₹1 lakh a call. In its untouched test (January 2025 to October 2026) it made "
        "+0.13% a call after costs, almost all of it on the short sells, with months of "
        "losses in between: this record shows whether that holds from here. Real orders, "
        "when switched on, differ: approved by 10:00, a stop, and out at 15:00.")
SIDE = {1: "buy", -1: "sell short"}


def page(conn) -> None:
    st.header("ML intraday calls")
    st.caption(NOTE)
    rec = ml.record(conn)
    t = rec["total"]
    m = st.columns(4)
    m[0].metric("Calls closed", t["trades"])
    m[1].metric("Net since the start", f"₹{t['net_inr']:+,.0f}")
    m[2].metric("Average a call", "n/a" if t["net_pct"] is None else f"{t['net_pct']:+.2f}%")
    m[3].metric("Profit factor", "n/a" if t["profit_factor"] is None
                else f"{t['profit_factor']:.2f}")
    st.caption(_luck(t))
    today = utc_now().astimezone(IST).date()
    rows = ml.picks(conn, today)
    st.subheader(f"Today, {today:%d %b %Y}")
    day = next((d for d in rec["days"] if d["session_date"] == today), None)
    if not rows:
        st.info(day["note"] if day and day["note"] else
                "No calls yet: the model runs at 09:45 on trading days.")
    else:
        st.dataframe(_table(rows), hide_index=True, width="stretch")
    if auth.is_admin():
        _orders(conn, today)
    if rec["days"]:
        days = pl.DataFrame(rec["days"]).with_columns(
            pl.col("net_inr").cum_sum().alias("since the start ₹"))
        st.subheader("Each day")
        st.line_chart(days.select("session_date", "since the start ₹"), x="session_date",
                      y="since the start ₹")
        st.dataframe(days.select(pl.col("session_date").alias("day"),
                                 pl.col("scored").alias("stocks scored"),
                                 pl.col("trades").alias("calls closed"),
                                 pl.col("net_inr").round(0).alias("net ₹"),
                                 "since the start ₹", "note").reverse(),
                     hide_index=True, width="stretch")
        older = [r for r in ml.picks(conn) if r["session_date"] != today]
        if older:
            st.subheader("Earlier calls")
            st.dataframe(_table(older), hide_index=True, width="stretch")


def _table(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame([{
        "day": r["session_date"], "stock": r["symbol"], "call": SIDE[r["side"]],
        "predicted %": round(r["prediction"] * 100, 2), "entry": r["entry"], "exit": r["exit"],
        "net ₹": None if r["net_inr"] is None else round(r["net_inr"]),
        "net %": None if r["net_pct"] is None else round(r["net_pct"], 2),
        "status": r["status"]} for r in rows])


def _luck(t: dict) -> str:
    """Whether the record can yet tell skill from luck."""
    n = t["trades"]
    if n < ml.CALLS_TO_TELL:
        return (f"{n} of about {ml.CALLS_TO_TELL} calls: an edge like the backtest's (+0.13% a "
                "call) needs about that many before it can be told apart from luck, so early "
                "results, good or bad, say little.")
    verdict = ("looks like skill" if t["t"] is not None and t["t"] >= 2 else
               "not distinguishable from luck yet" if t["t"] is not None and t["t"] > 0 else
               "no edge so far")
    return (f"{n} calls; the record's t-statistic is {t['t']:.2f} (2 or more suggests a real "
            f"edge): {verdict}.")


ORDERS_NOTE = (f"A real order enters at the live price (a limit at most 0.1% past it), with a "
               f"stop at half the 14-day ATR, and the app squares it off at "
               f"{ML_SQUARE_OFF:%H:%M} IST: Upstox closes intraday positions in stocks in NSE's "
               "closing auction at 15:10 and takes no orders in them after 15:09, so the "
               "15:15 exit of the paper record can't be had. Tested that way on the same "
               "years (research/ml_live), it made +0.05% to +0.08% a trade after charges "
               "entered at 09:50 and +0.01% to +0.06% at 10:00, less than the paper rule; "
               "every minute of delay costs. Charges take most of that below ₹50,000 a trade. "
               "Your amount per trade, maximum loss, price deviation and daily limits apply.")


def _orders(conn, today) -> None:
    """Real orders for today's calls: approve one, and those placed."""
    st.subheader("Real orders")
    st.caption(ORDERS_NOTE)
    cfg = trade_settings(conn)
    now = utc_now()
    calls = [r for r in ml.picks(conn, today) if r["status"] == "open"]
    ordered = {r[0] for r in conn.execute(
        "select symbol from intraday_trade where trading_day=%s and status<>'rejected'",
        (today,)).fetchall()}
    if not (cfg["enabled"] and cfg["ml_orders_enabled"]):
        st.info("Real orders for the ML calls are off. Turn on live trading and \"Real orders "
                "for the ML calls\" in Settings, Intraday trading tab.")
    elif not trading_token():
        st.info("Save a trading OAuth token in Settings, Upstox tab, first.")
    elif not ml_window(now):
        st.info(f"Calls can be approved from their decision at 09:45 until "
                f"{ML_APPROVE_UNTIL:%H:%M} IST on trading days.")
    elif not [r for r in calls if r["symbol"] not in ordered]:
        st.info("No call left to approve today.")
    else:
        open_calls = {r["symbol"]: r for r in calls if r["symbol"] not in ordered}
        with st.form("approve_ml_order"):
            symbol = st.selectbox("Today's call", list(open_calls), format_func=lambda k: (
                f"{k} · {SIDE[open_calls[k]['side']].upper()} · predicted "
                f"{open_calls[k]['prediction'] * 100:+.2f}%"))
            pick = ml.pick(conn, today, symbol)
            preview = ml_preview(pick, cfg, intraday_rates())
            if preview["ok"]:
                st.write(f"At the price the model saw, ₹{float(pick['decision_price']):.2f}: "
                         f"{preview['quantity']} shares (₹{preview['value']:,.0f}), stop "
                         f"₹{preview['stop']:.2f}; loss at the stop ₹{preview['loss']:,.0f}, "
                         f"charges about ₹{preview['charges']:,.0f}. Repriced at the live "
                         "price when you approve.")
                if preview["value"] < ML_MIN_USEFUL_RUPEES:
                    st.warning(f"At ₹{preview['value']:,.0f} a trade, charges take most of the "
                               "tested edge: it was tested at ₹1 lakh. Raise the amount per "
                               "trade and the maximum loss in Settings to trade it as tested.")
            else:
                st.warning(f"At your settings no order can be placed: {preview['reason']}.")
            confirmed = st.checkbox("I approve this specific intraday order with real money")
            if st.form_submit_button("Place the order", type="primary"):
                auth.require_access(admin=True)
                if not confirmed:
                    st.error("Confirm this specific order first.")
                else:
                    try:
                        trade_id, state = approve_ml(conn, pick["instrument_key"],
                                                     source="admin")
                    except TradeError as exc:
                        st.error(str(exc))
                    else:
                        st.success(f"Trade #{trade_id}: {state}. Verify the fill and its stop "
                                   "in Upstox.")
    rows = conn.execute("""select symbol, action, quantity, entry_price, stop_price, status,
                                  entry_fill, exit_fill, net_pnl, pnl_note, approved_by,
                                  approved_at
                           from intraday_trade where kind = 'ml' and trading_day >= %s
                           order by trade_id desc limit 100""",
                        (today.replace(day=1),)).fetchall()
    if rows:
        st.dataframe([dict(zip(("stock", "side", "shares", "limit ₹", "stop ₹", "status",
                                "entry fill ₹", "exit fill ₹", "net P&L ₹", "note",
                                "approved by", "approved at"), r, strict=True)) for r in rows],
                     hide_index=True, width="stretch")
    else:
        st.caption("No real order for an ML call this month.")
