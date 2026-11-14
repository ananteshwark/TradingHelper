"""The recommendation tracker page (igs.tracking, docs/TRACKING.md): every recommendation
with the market at the moment it was made, and its price and volume since."""
from __future__ import annotations

import polars as pl
import streamlit as st

from igs import tracking
from igs.timeutil import IST
from igs.ui import charts

NOTE = ("Every recommendation the app makes: the 09:45 model's calls, the evening picks, the "
        "spike picks, the scanner's calls, the AI's buy and sell calls and the momentum paper "
        "tracks' holdings. Each is priced from Upstox within a minute of being made (after "
        "the close, at the close), with the day's volume, VWAP and the Nifty 50, and then "
        "followed every 5 minutes (intraday) or 30 minutes (longer ones). Moves are in % from "
        "that price, signed for the side: + is in its favour. Worst and best are the worst "
        "and best moves seen at the ticks. The shadow columns are research/adverse's models "
        "for the 09:45 calls and evening picks; none passed its holdout, so they change "
        "nothing (docs/TRACKING.md).")
SIDE = {1: 'buy', -1: 'short sell'}


def _theme() -> str:
    try:
        kind = st.context.theme.type
    except AttributeError:
        kind = None
    return kind if kind in ('light', 'dark') else 'light'


def _ist(ts):
    return ts.astimezone(IST).replace(tzinfo=None) if ts else None


def _f(v, digits=2):
    return round(float(v), digits) if v is not None else None


def page(conn, title: bool = True) -> None:
    if title:
        st.header('Tracker')
    st.caption(NOTE)
    names = dict(tracking.SOURCE_NAMES)
    chosen = st.multiselect('Sources', list(names), default=list(names),
                            format_func=names.get, key='tracker_sources')
    cur = conn.execute("""select rec_id, source, symbol, side, horizon, recommended_at,
                                 source_price, rec_price, captured_at, capture, capture_note,
                                 rec_rvol, rec_nifty, last_price, last_at, mae_pct, mae_at,
                                 rvol_at_mae, mfe_pct, mfe_at, ticks, status, shadow
                          from rec_track where source = any(%s)
                          order by recommended_at desc, rec_id desc limit 500""",
                       (chosen or ['none'],))
    columns = [c.name for c in cur.description]
    recs = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]
    if not recs:
        st.info('None yet: recommendations appear here within a minute of being made, once '
                '`igs-track.timer` is installed (scripts/install-schedules.sh) and an Upstox '
                'access token is set (Settings, Upstox tab).')
        return
    rows = []
    for r in recs:
        base = r['rec_price'] or r['source_price']
        now = (r['side'] * (float(r['last_price']) / float(base) - 1) * 100
               if r['last_price'] is not None and base else None)
        lag = ((r['captured_at'] - r['recommended_at']).total_seconds() / 60
               if r['captured_at'] else None)
        sh = r['shadow'] or {}
        a1 = sh.get('a1') or {}
        rows.append({
            'recommended (IST)': _ist(r['recommended_at']),
            'source': names[r['source']], 'stock': r['symbol'], 'side': SIDE[r['side']],
            'price then': _f(r['rec_price']), 'captured': r['capture'],
            'minutes late': round(lag, 1) if lag is not None else None,
            'volume vs usual then': _f(r['rec_rvol']),
            'latest': _f(r['last_price']), 'now %': _f(now),
            'worst %': _f(r['mae_pct']), 'worst at': _ist(r['mae_at']),
            'volume vs usual at worst': _f(r['rvol_at_mae']),
            'best %': _f(r['mfe_pct']), 'best at': _ist(r['mfe_at']),
            'ticks': r['ticks'], 'status': r['status'],
            'shadow net %': _f(a1['net'] * 100, 3) if 'net' in a1 else None,
            'shadow stop chance': _f(a1.get('stop_p')),
            'shadow veto': a1.get('veto'),
            'note': r['capture_note'] or sh.get('error', '')})
    frame = pl.DataFrame(rows, infer_schema_length=None, schema_overrides={
        c: pl.Float64 for c in ('price then', 'minutes late', 'volume vs usual then', 'latest',
                                'now %', 'worst %', 'volume vs usual at worst', 'best %',
                                'shadow net %', 'shadow stop chance')})
    summary = (frame.group_by('source', 'side')
               .agg(pl.len().alias('recommendations'),
                    pl.col('worst %').median().round(2).alias('median worst %'),
                    pl.col('best %').median().round(2).alias('median best %'),
                    (pl.col('worst %') <= -1).mean().mul(100).round(0)
                    .alias('% that went 1% or more against'),
                    pl.col('now %').median().round(2).alias('median now %'))
               .sort('source', 'side'))
    st.subheader('By source')
    st.dataframe(summary, hide_index=True)
    st.subheader('Recommendations')
    st.dataframe(frame, hide_index=True)
    labels = {r['rec_id']: f"{_ist(r['recommended_at']):%d %b %H:%M} · {names[r['source']]} · "
              f"{r['symbol']} {SIDE[r['side']]}" for r in recs if r['ticks']}
    if not labels:
        return
    rec_id = st.selectbox('Path of', list(labels), format_func=labels.get, key='tracker_rec')
    rec = next(r for r in recs if r['rec_id'] == rec_id)
    base = float(rec['rec_price'] or rec['source_price'])
    ticks = [{'at': _ist(at), 'move %': rec['side'] * (float(p) / base - 1) * 100,
              'volume vs usual': _f(rv), 'Nifty %': (float(n) / float(rec['rec_nifty']) - 1)
              * 100 if n is not None and rec['rec_nifty'] else None}
             for at, p, rv, n in conn.execute(
                 'select at, price, rvol, nifty from rec_tick where rec_id = %s order by at',
                 (rec_id,)).fetchall()]
    theme = _theme()
    st.altair_chart(charts.move_chart(ticks, theme), width='stretch')
    if any(t['volume vs usual'] is not None for t in ticks):
        st.altair_chart(charts.volume_chart(ticks, theme), width='stretch')
    own = (f"; the source's own price ₹{float(rec['source_price']):,.2f}"
           if rec['source_price'] is not None else '')
    st.caption(f"Recommended {_ist(rec['recommended_at']):%d %b %Y %H:%M} IST at ₹{base:,.2f}"
               f"{own}. {rec['ticks']} ticks, the last at {_ist(rec['last_at']):%d %b %H:%M}.")
