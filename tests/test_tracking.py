"""The recommendation tracker (igs.tracking, docs/TRACKING.md)."""

from __future__ import annotations

import datetime as dt

from psycopg.types.json import Jsonb
from test_intraday import seed_stock

from igs import tracking
from igs.intraday.engine import Candle
from igs.timeutil import IST

DAY = dt.date(2026, 11, 13)                  # a Friday
DECIDED = dt.datetime(2026, 11, 13, 9, 46, 10, tzinfo=IST)
KEY, KEY2 = 'NSE_EQ|INE000000001', 'NSE_EQ|INE000000002'


def _quote(price, volume=1000, vwap=None, change=1.0, token=KEY, high=None, low=None):
    return {'instrument_token': token, 'last_price': price, 'volume': volume,
            'average_price': vwap or price, 'net_change': change,
            'ohlc': {'open': price, 'high': high or price, 'low': low or price,
                     'close': price - change}}


def test_quotes_give_price_volume_vwap_range_and_previous_close():
    got = tracking.parse_quotes({
        'NSE_EQ:A': _quote(101.0, 5000, 100.5, 1.0, KEY, 102, 99),
        'NSE_EQ:B': {'instrument_token': KEY2, 'last_price': 0},          # no trade: left out
        'bad': 'x'})
    assert got == {KEY: {'price': 101.0, 'volume': 5000, 'vwap': 100.5, 'open': 101.0,
                         'high': 102.0,
                         'low': 99.0, 'prev_close': 100.0}}


def _candles(days, per_candle):
    out = []
    for n in range(days):
        day = DAY - dt.timedelta(days=n + 1)
        start = dt.datetime.combine(day, dt.time(9, 15), IST)
        out += [Candle(start + dt.timedelta(minutes=5 * i), 1, 1, 1, 1, per_candle)
                for i in range(75)]
    return out


def test_relative_volume_compares_the_day_so_far_with_the_same_time_on_usual_days():
    assert tracking.volume_curve(_candles(9, 100)) is None       # fewer than 10 sessions
    curve = tracking.volume_curve(_candles(12, 100))
    assert curve[0] == 100 and curve[5] == 600 and curve[-1] == 7500
    at = dt.datetime.combine(DAY, dt.time(9, 45), IST)          # six candles closed
    assert tracking.relative_volume(curve, 1200, at) == 2.0
    assert tracking.relative_volume(curve, 1200, at.replace(hour=9, minute=16)) is None
    assert tracking.relative_volume(None, 1200, at) is None


def _row(**kw):
    row = {'horizon': 'intraday', 'track_from': DECIDED,
           'track_until': dt.datetime.combine(DAY, dt.time(15, 15), IST), 'last_at': None,
           'side': 1, 'rec_price': 100.0, 'source_price': None, 'mae_pct': None,
           'mfe_pct': None, 'ticks': 0}
    return {**row, **kw}


def test_intraday_ticks_every_five_minutes_and_longer_ones_every_thirty_then_daily():
    now = DECIDED + dt.timedelta(minutes=10)
    assert tracking.due(_row(), now)
    assert not tracking.due(_row(last_at=now - dt.timedelta(minutes=3)), now)
    assert tracking.due(_row(last_at=now - dt.timedelta(minutes=5)), now)
    assert not tracking.due(_row(), DECIDED - dt.timedelta(minutes=1))       # not started
    assert not tracking.due(_row(), dt.datetime.combine(DAY, dt.time(15, 20), IST))
    swing = _row(horizon='swing', track_until=DECIDED + dt.timedelta(days=200))
    assert not tracking.due({**swing, 'last_at': now - dt.timedelta(minutes=20)}, now)
    assert tracking.due({**swing, 'last_at': now - dt.timedelta(minutes=30)}, now)
    later = DECIDED + dt.timedelta(days=40)
    assert not tracking.due({**swing, 'last_at': later - dt.timedelta(hours=5)}, later)
    close = later.replace(hour=15, minute=25)
    assert tracking.due({**swing, 'last_at': close - dt.timedelta(days=1)}, close)
    assert not tracking.due({**swing, 'last_at': close - dt.timedelta(minutes=2)}, close)
    assert not tracking.due({**swing, 'last_at': close - dt.timedelta(days=1)},
                            close.replace(hour=14))       # once a day, near the close


def test_the_path_is_signed_for_the_side():
    at = DECIDED + dt.timedelta(minutes=5)
    short = _row(side=-1)
    up = tracking.update_path(short, 102.0, at, 1.5)
    assert round(up['mae_pct'], 6) == -2.0 and up['rvol_at_mae'] == 1.5
    assert round(up['mfe_pct'], 6) == -2.0 and up['ticks'] == 1
    down = tracking.update_path({**short, **up}, 99.0, at + dt.timedelta(minutes=5), 0.8)
    assert 'mae_pct' not in down and round(down['mfe_pct'], 6) == 1.0
    # No price captured: the first tick's price becomes the base.
    first = tracking.update_path(_row(rec_price=None, source_price=60.0), 50.0, at, None)
    assert (first['rec_price'], first['mae_pct'], first['mfe_pct']) == (50.0, 0.0, 0.0)


def _pick(conn, symbol='AAA', key=KEY, side=1, price=100.0):
    conn.execute("""insert into ml_intraday_day (session_date, decided_at, model, universe,
                        scored) values (%s, %s, 'test', 200, 150) on conflict do nothing""",
                 (DAY, DECIDED))
    conn.execute("""insert into ml_intraday_pick (session_date, instrument_key, symbol, side,
                        prediction, rank, features, entry_after, status, decision_price)
                    values (%s, %s, %s, %s, 0.004, 1, %s, %s, 'open', %s)""",
                 (DAY, key, symbol, side, Jsonb({}), DECIDED, price))
    conn.commit()


def _stub(prices):
    calls = []

    def quotes(keys):
        calls.append(list(keys))
        return {k: tracking.parse_quotes({'x': _quote(prices[k], token=k)})[k]
                for k in keys if k in prices}
    quotes.calls = calls
    return quotes


def test_a_call_is_registered_once_with_the_market_at_that_moment(db_conn):
    _pick(db_conn)
    now = DECIDED + dt.timedelta(seconds=40)
    quotes = _stub({KEY: 100.4, tracking.NIFTY: 24000.0})
    assert tracking.register(db_conn, now, quotes=quotes)['registered'] == 1
    assert tracking.register(db_conn, now, quotes=quotes)['registered'] == 0
    row = db_conn.execute("""select source, ref, side, horizon, rec_price, rec_nifty, capture,
                                    capture_note, source_price, captured_at
                             from rec_track""").fetchone()
    assert row[:4] == ('ml_0945', f'{DAY}|{KEY}', 1, 'intraday')
    assert (float(row[4]), float(row[5]), row[6], row[7]) == (100.4, 24000.0, 'live', '')
    assert float(row[8]) == 100.0 and row[9] == now


def test_a_late_or_failed_capture_says_so(db_conn):
    _pick(db_conn)
    _pick(db_conn, 'BBB', KEY2, -1)
    later = DECIDED + dt.timedelta(minutes=7)
    tracking.register(db_conn, later, quotes=_stub({KEY: 100.0}))
    got = dict(db_conn.execute('select symbol, capture || \': \' || capture_note '
                               'from rec_track').fetchall())
    assert got == {'AAA': 'live: Captured 7 minutes after the recommendation',
                   'BBB': 'failed: No quote for this stock'}


def test_ticks_follow_the_price_until_the_horizon_ends(db_conn):
    _pick(db_conn, side=-1)
    tracking.register(db_conn, DECIDED, quotes=_stub({KEY: 100.0}))
    for minutes, price in ((5, 101.0), (7, 105.0), (10, 98.0)):
        tracking.tick(db_conn, DECIDED + dt.timedelta(minutes=minutes),
                      quotes=_stub({KEY: price, tracking.NIFTY: 24000.0}))
    ticks = db_conn.execute('select price from rec_tick order by at').fetchall()
    assert [float(p) for (p,) in ticks] == [101.0, 98.0]           # 7 minutes was not due
    mae, mfe, n = db_conn.execute('select mae_pct, mfe_pct, ticks from rec_track').fetchone()
    assert (round(mae, 6), round(mfe, 6), n) == (-1.0, 2.0, 2)
    assert tracking.tick(db_conn, dt.datetime.combine(DAY, dt.time(15, 40), IST),
                         quotes=_stub({KEY: 1.0}))['ticks'] == 0     # market closed
    assert tracking.finish(db_conn, dt.datetime.combine(DAY, dt.time(15, 40), IST)) == 1
    assert db_conn.execute('select status from rec_track').fetchone()[0] == 'done'


def test_a_quote_failure_is_reported_and_retried_next_minute(db_conn):
    _pick(db_conn)

    def broken(keys):
        raise tracking.QuoteError('Upstox returned HTTP 503')
    text = tracking.step(db_conn, clock=lambda: DECIDED, quotes=broken)
    assert text == ('Tracking: registered 1 (0 captured); '
                    'quotes failed: Upstox returned HTTP 503')
    row = db_conn.execute('select capture, capture_note, rec_price from rec_track').fetchone()
    assert row == ('failed', 'Upstox returned HTTP 503', None)
    # The next minute's tick gets a quote: the path starts from its price.
    later = DECIDED + dt.timedelta(minutes=1)
    assert tracking.step(db_conn, clock=lambda: later, quotes=_stub({KEY: 99.0}),
                         universe=lambda conn: []) == 'Tracking: 1 ticks'
    got = db_conn.execute('select rec_price, mae_pct, ticks from rec_track').fetchone()
    assert (float(got[0]), got[1], got[2]) == (99.0, 0.0, 1)


# --------------------------------------------------------------------------- shadow models


def test_the_tree_evaluator_routes_missing_values_as_lightgbm_does():
    from igs.tracking_models import Trees
    # x0 <= 1 -> left; missing x0 goes right (missing 2, default right).
    t = Trees({'features': ['a', 'b'], 'link': 'regression',
               'trees': [[0, 1.0, 0.5, [1, 0.0, -1.0, 2.0, True, 1], False, 2]]})
    assert t.predict({'a': 0.5}) == 0.5
    assert t.predict({'a': 3.0, 'b': -1.0}) == -1.0
    assert t.predict({'a': 3.0, 'b': 0.0}) == -1.0          # zero is missing: default left
    assert t.predict({'a': None, 'b': 5.0}) == 2.0          # missing a: default right
    p = Trees({'features': ['a'], 'link': 'logistic', 'trees': [0.0]}).predict({})
    assert p == 0.5


def test_inputs_are_signed_for_the_side_as_the_research_did():
    from igs.tracking_models import signed
    raw = {'gap': 0.01, 'clv30': 0.8, 'breadth': 0.7, 'note': 'x', 'none': None}
    assert signed(raw, 1) == {'s_gap': 0.01, 's_clv30': 0.8, 's_breadth': 0.7}
    short = signed(raw, -1)
    assert short['s_gap'] == -0.01 and round(short['s_clv30'], 9) == 0.2
    assert round(short['s_breadth'], 9) == 0.3


def test_path_parameters_come_from_the_ticks_since_the_entry():
    from igs.tracking_models import path_row
    entry = {'at': DECIDED, 'price': 100.0, 'volume': 1000, 'nifty': 24000.0}
    ticks = [{'at': DECIDED + dt.timedelta(minutes=m), 'price': p}
             for m, p in ((5, 99.0), (10, 101.0), (15, 100.5))]
    now = {'at': DECIDED + dt.timedelta(minutes=15), 'price': 100.5, 'volume': 4000,
           'vwap': 100.0, 'day_high': 102.0, 'day_low': 98.0, 'nifty': 24240.0}
    curve = [100.0 * (i + 1) for i in range(75)]
    row = path_row(entry, ticks, now, curve, -1, 0.02, 15)
    assert round(row['move'], 6) == -0.005 and round(row['move_atr'], 6) == -0.25
    assert round(row['best_atr'], 6) == 0.5 and round(row['worst_atr'], 6) == -0.5
    assert round(row['last15'], 6) == -0.005                 # from the entry: 15 minutes
    assert round(row['vwap_gap'], 6) == -0.005
    assert round(row['range_pos'], 6) == 0.375               # 1 - (100.5-98)/4
    assert round(row['nifty_move'], 6) == -0.01 and round(row['rel_move'], 6) == 0.005
    assert row['rvol_since'] == 3000 / 300                   # 09:46 to 10:01: slots 5 to 8


def _ml_pick_with_features(conn, side=1):
    conn.execute("""insert into ml_intraday_day (session_date, decided_at, model, universe,
                        scored) values (%s, %s, 'test', 200, 150)""", (DAY, DECIDED))
    feats = {'gap': 0.004, 'r30': 0.006, 'prev_r1': 0.01, 'r5': 0.02, 'range30_atr': 0.9,
             'rvol30': 2.5, 'vwap_dist': 0.002, 'clv30': 0.7, 'prev_clv': 0.6,
             'prev_last_hour': 0.001, 'prev_intraday': 0.004, 'r21': 0.05, 'atr_pct': 0.02,
             'dist20h': -0.01, 'dist20l': 0.08, 'log_value20': 21.5, 'ovn5': 0.01,
             'intra5': 0.01, 'n_gap': 0.001, 'n_r30': 0.001, 'n_prev_r1': 0.002,
             'n_r5': 0.004, 'rel30': 0.005, 'value20': 2.2e9}
    for key, sym, pred in ((KEY, 'AAA', 0.006 * side), (KEY2, 'BBB', 0.004 * side)):
        conn.execute("""insert into ml_intraday_pick (session_date, instrument_key, symbol,
                            side, prediction, rank, features, entry_after, status,
                            decision_price)
                        values (%s, %s, %s, %s, %s, 1, %s, %s, 'open', 100)""",
                     (DAY, key, sym, side, pred, Jsonb(feats), DECIDED))
    conn.commit()


def test_a_call_gets_shadow_predictions_at_the_entry_and_at_its_checkpoints(db_conn):
    _ml_pick_with_features(db_conn)
    tracking.register(db_conn, DECIDED, quotes=_stub({KEY: 100.0, KEY2: 50.0}))
    universe = [f'NSE_EQ|U{i:03d}' for i in range(60)]
    prices = {KEY: 100.0, KEY2: 50.0, tracking.NIFTY: 24000.0,
              **{k: 10.0 for k in universe}}
    for minutes in range(0, 50, 5):
        prices[KEY] = 100.0 + minutes * 0.02
        tracking.tick(db_conn, DECIDED + dt.timedelta(minutes=minutes),
                      quotes=_stub(prices), universe=lambda conn: universe)
    shadow = dict(db_conn.execute("select symbol, shadow from rec_track").fetchall())
    a, b = shadow['AAA'], shadow['BBB']
    assert a['model'] == 'adverse-m0945-v1' and a['decision']['rank'] == 1
    assert b['decision']['rank'] == 2 and b['decision']['conviction'] == 0.004
    assert a['decision']['s_breadth'] == 1.0                 # all 60 above their close
    assert 0 < a['a1']['stop_p'] < 1 and a['a1']['veto'] == (a['a1']['net'] < 0)
    assert sorted(a['a2']) == ['1', '2', '3']                 # 15, 30 and 45 minutes
    assert all(0 < c['stop_p'] < 1 for c in a['a2'].values())


def test_evening_picks_are_followed_the_next_session_and_momentum_holdings_for_a_month(
        db_conn):
    signal = dt.date(2026, 11, 13)                       # Friday: they trade on Monday
    decided = dt.datetime(2026, 11, 13, 15, 50, tzinfo=IST)
    db_conn.execute("""insert into evening_day (signal_date, decided_at, model, universe,
                           scored) values (%s, %s, 'test', 200, 150)""", (signal, decided))
    db_conn.execute("""insert into evening_pick (signal_date, instrument_key, symbol, side,
                           prediction, rank, features, status, decision_price)
                       values (%s, %s, 'AAA', -1, -0.004, 1, '{}', 'open', 100)""",
                    (signal, KEY))
    cid = seed_stock(db_conn, 'MOM', 'INE000000009')
    db_conn.execute("""insert into momentum_rebalance (track, signal_date, entry_date, universe,
                           holdings, created_at) values ('ranking', '2026-10-30', '2026-11-02',
                           180, %s, %s)""",
                    (Jsonb([{'company_id': cid, 'symbol': 'MOM', 'rank': 1, 'score_pct': 99}]),
                     decided - dt.timedelta(days=1)))
    db_conn.commit()
    tracking.register(db_conn, decided + dt.timedelta(minutes=1),
                      quotes=_stub({KEY: 101.0, 'NSE_EQ|INE000000009': 500.0}))
    got = {r[0]: r[1:] for r in db_conn.execute(
        """select source, side, horizon, capture, track_from, track_until, instrument_key
           from rec_track order by source""").fetchall()}
    monday = dt.datetime(2026, 11, 16, 9, 20, tzinfo=IST)
    assert got['evening'][:5] == (-1, 'intraday', 'close', monday,
                                  monday.replace(hour=15, minute=15))
    start = dt.datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    assert got['momentum'] == (1, 'swing', 'close', start, start + dt.timedelta(days=35),
                               'NSE_EQ|INE000000009')


def test_the_tracker_page_lists_recommendations_and_plots_a_path(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.tracker import page
page(connect(autocommit=True))''', default_timeout=30)
    at.run()
    assert not at.exception and 'None yet' in at.info[0].value
    _ml_pick_with_features(db_conn, side=-1)
    tracking.register(db_conn, DECIDED, quotes=_stub({KEY: 100.0, KEY2: 50.0}))
    for minutes, price in ((0, 100.0), (5, 101.0), (10, 99.0)):
        tracking.tick(db_conn, DECIDED + dt.timedelta(minutes=minutes),
                      quotes=_stub({KEY: price, KEY2: 50.0, tracking.NIFTY: 24000.0}),
                      universe=lambda conn: [])
    at.run()
    assert not at.exception, at.exception
    summary, recs = at.dataframe[0].value, at.dataframe[1].value
    assert list(summary['recommendations']) == [2]
    aaa = recs[recs['stock'] == 'AAA'].iloc[0]
    assert (aaa['side'], aaa['worst %'], aaa['best %'], aaa['now %']) == (
        'short sell', -1.0, 1.0, 1.0)
    assert aaa['shadow stop chance'] is not None
    assert at.selectbox(key='tracker_rec').options
    # The path's chart; no volume chart without the stock's usual volume.
    assert [e.type for e in at.main if getattr(e, 'type', '') == 'vega_lite_chart'] == [
        'vega_lite_chart']


def test_the_app_has_a_tracker_page(db_conn, monkeypatch):
    import os
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    app = Path(__file__).resolve().parents[1] / 'src/igs/ui/app.py'
    at = AppTest.from_file(str(app), default_timeout=60).run()
    at.sidebar.radio(key='page').set_value('Tracker').run()
    assert not at.exception, at.exception
    assert [h.value for h in at.header] == ['Tracker'] and 'None yet' in at.info[0].value
