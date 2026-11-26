"""The Winning traders page (igs.deal_traders): the clients in NSE's bulk deal files whose
same-day bets keep winning, what they carry overnight, and the paper trades that follow it
(igs.follow_winners)."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import deal_traders, follow_winners

NOTE = ("NSE's bulk deal file names every client whose trades in a stock add up to more than "
        "0.5% of its shares in a day, with what it bought and sold and at what average "
        "prices. A client that bought and sold the same stock that day made a same-day bet "
        "the file shows whole: its result is the quantity times the sell price less the buy "
        "price, before charges. Each client's record keeps every bet since the app began "
        "collecting the files (every 15 minutes, igs-intraday-deals.timer).\n\n"
        "**Consistent winners** have the most bets with the fewest losses that luck "
        "wouldn't explain; they are named once on Telegram (the deals chat, Settings > "
        "Alerts, `winning_traders`). Most turn out to be market makers and arbitrage "
        "traders earning a small spread many times over.\n\n"
        "**They can't be copied the same day:** the file appears after the close, once the "
        "bet is over. The last column measures what can be followed: what they carried "
        "overnight, the next session against the Nifty 500 (docs/DEAL_TRADERS.md).")

FOLLOW_NOTE = ("When a consistent winner carries shares at the close, the next session is "
               "traded on paper their way: bought, or sold short, at the open and closed at "
               "the close, about Rs 1 lakh each, with 0.05% slippage each way and Upstox's "
               "intraday charges. Only trades that could have been placed count: the winner "
               "was named before the file was read, and the session had not opened. The "
               "trades go to the paper chat on Telegram before the open, the results after "
               "the close (Settings > Alerts, `follow_winners`). Nothing here places orders.")
SIDE = {1: 'buy', -1: 'short sale'}


def page(conn) -> None:
    st.header("Winning traders")
    st.caption(NOTE)
    r = deal_traders.rule()
    everyone = deal_traders.traders(conn)
    if everyone:
        _winners(everyone, r)
    else:
        st.info("None yet: same-day bets appear once the deals collection has read a bulk "
                "deal file in which a client bought and sold the same stock.")
    _following(conn)


def _winners(everyone: list[dict], r: dict) -> None:
    min_bets = st.number_input("At least this many same-day bets", min_value=1,
                               value=int(r['min_bets']), step=1, key='traders_min_bets')
    rows = [{'client': t['client'], 'who': t['what'],
             'consistent winner': deal_traders.consistent(t, r),
             'bets': t['bets'], 'won': t['wins'], 'won %': round(t['win_rate'] * 100, 1),
             'chance by luck': t['p_value'],
             'result (Rs crore, before charges)': round(t['pnl_inr'] / 1e7, 2)
             if t['pnl_inr'] is not None else None,
             'average a round trip %': round(t['avg_ret_pct'], 3)
             if t['avg_ret_pct'] is not None else None,
             'stocks': t['stocks'], 'first': t['first'], 'last': t['last'],
             'carried overnight (times)': t['carried'],
             'carried: next session vs Nifty 500 %': round(t['carried_next_pct'], 2)
             if t['carried_next_pct'] is not None else None}
            for t in everyone if t['bets'] >= min_bets]
    st.caption(f"{sum(r_['consistent winner'] for r_ in rows)} consistent winners among "
               f"{len(rows)} clients with {min_bets} or more same-day bets; "
               f"{len(everyone)} clients in all.")
    if rows:
        st.dataframe(pl.DataFrame(rows, infer_schema_length=None), hide_index=True,
                     column_config={'chance by luck': st.column_config.NumberColumn(
                         format='%.2g')})


def _following(conn) -> None:
    st.subheader("Following them (paper)")
    st.caption(FOLLOW_NOTE)
    rec = follow_winners.record(conn)
    a, b, c, d = st.columns(4)
    a.metric("Trades", rec['trades'])
    b.metric("Won", f"{rec['won'] / rec['trades']:.0%}" if rec['trades'] else "–")
    c.metric("After charges", f"Rs {rec['net_inr']:+,.0f}")
    d.metric("t", f"{rec['t']:.2f}" if rec['t'] is not None else "–")
    st.caption("Real orders: " + follow_winners.bar_text(rec))
    rows = conn.execute("""select signal_date, symbol, side, clients, carried_inr::float8,
            session, status, open_price::float8, close_price::float8, quantity,
            charges_inr::float8, net_inr::float8, net_pct::float8
        from follow_trade order by signal_date desc, trade_id desc limit 300""").fetchall()
    if not rows:
        st.info("No paper trades yet: one opens when a consistent winner carries shares at "
                "the close.")
        return
    st.dataframe(pl.DataFrame([{
        'deal file': r[0], 'stock': r[1], 'side': SIDE[r[2]], 'winners': r[3],
        'carried (Rs crore)': round(r[4] / 1e7, 2), 'session': r[5], 'status': r[6],
        'open': r[7], 'close': r[8], 'shares': r[9],
        'charges (Rs)': round(r[10], 2) if r[10] is not None else None,
        'after charges (Rs)': round(r[11], 2) if r[11] is not None else None,
        'after charges %': round(r[12], 3) if r[12] is not None else None}
        for r in rows], infer_schema_length=None), hide_index=True)
