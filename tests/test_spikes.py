"""The spike watcher and the spike-reversal paper track (igs.intraday.spikes,
research/spike_reversal/PAPER.md): the rule from closed candles, the band rule, costs, and a
day of scans against a made-up Upstox feed. No request leaves the machine."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from igs.intraday import spikes
from igs.intraday.engine import Candle
from igs.intraday.upstox import Upstox
from igs.timeutil import IST

DAY = dt.date(2026, 10, 28)                      # a Wednesday


def at(h, m, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m), IST)


def bars(spec):
    """Candles from 09:15, five minutes apart, from (open, high, low, close)."""
    start = at(9, 15)
    return [Candle(start + dt.timedelta(minutes=5 * i), o, h, lo, c, 1000)
            for i, (o, h, lo, c) in enumerate(spec)]


def rise():
    """Flat at 100 to 09:55, up to 113 by the 10:25 close (13.1% above the 10:00 low), a
    flat 10:30 candle, then down 0.5 a candle from the 10:35 open of 112.5."""
    spec = [(100, 100.3, 99.7, 100)] * 9
    path = [100, 103, 106, 109, 111, 113]
    spec += [(a, b + 0.2, a - 0.1, b) for a, b in zip([100] + path[:-1], path, strict=True)]
    spec.append((113, 113.6, 112.8, 113.2))
    for k in range(75 - len(spec)):
        o = 112.5 - 0.5 * k
        spec.append((o, o + 0.3, o - 0.7, o - 0.5))
    return bars(spec)


def fall():
    """Flat at 200, down to 175 by the 10:25 close, a flat candle, then a slow drift up that
    never reaches the 4% target: out at the 15:15 open."""
    spec = [(200, 200.5, 199.5, 200)] * 9
    path = [197, 194, 188, 182, 178, 175]
    spec += [(a, a + 0.1, b - 0.2, b) for a, b in zip([200] + path[:-1], path, strict=True)]
    spec.append((175, 176, 174.8, 175.5))
    for k in range(75 - len(spec)):
        o = 175.4 + 0.05 * k
        spec.append((o, o + 0.15, o - 0.1, o + 0.05))
    return bars(spec)


def closed(candles, now):
    return [b for b in candles if b.start + spikes.BAR <= now]


def test_the_spike_the_flat_candle_and_the_target():
    day = rise()
    assert spikes.spike(day, 0.12) == (14, -1, 9)          # the 10:25 candle, a rise
    assert spikes.flat_candle(day, 14) == 15               # the 10:30 candle
    assert spikes.trade(closed(day, at(10, 32)), at(10, 32))['status'] == 'waiting'
    t = spikes.trade(closed(day, at(10, 42)), at(10, 42))
    assert (t['status'], t['side'], t['entry']) == ('open', -1, 112.5)
    assert t['target'] == pytest.approx(112.5 * 0.9985 * 0.96, abs=1e-4)
    t = spikes.trade(closed(day, at(15, 21)), at(15, 21))
    assert (t['status'], t['exit_reason'], t['exit_bar']) == ('closed', 'target', at(11, 15))
    assert t['exit'] == pytest.approx(112.5 * 0.9985 * 0.96, abs=1e-4)


def test_a_buy_after_a_fall_goes_out_at_the_1515_open():
    day = fall()
    t = spikes.trade(closed(day, at(15, 21)), at(15, 21))
    assert (t['side'], t['entry'], t['exit_reason']) == (1, 175.4, 'time')
    assert t['exit_bar'] == at(15, 15) and t['exit'] == pytest.approx(175.4 + 0.05 * 56)


def test_no_spike_no_flat_candle_and_a_lock():
    quiet = bars([(100, 100.5, 99.5, 100)] * 75)
    assert spikes.trade(closed(quiet, at(14, 0)), at(14, 0)) == {'status': 'watching'}
    assert spikes.trade(closed(quiet, at(15, 1)), at(15, 1)) == {'status': 'none'}
    # After the spike every candle moves 2% or is locked (no range): no flat candle.
    day = rise()[:16]
    day += [Candle(day[-1].start + spikes.BAR * (k + 1), 113, 113, 113, 113, 0)
            for k in range(59)]
    day[15] = Candle(day[15].start, 113, 116, 112, 115.3, 1000)
    t = spikes.trade(closed(day, at(15, 21)), at(15, 21))
    assert t['status'] == 'no flat' and 'entry' not in t


def test_the_band_rule():
    bands = {'UP': 20, 'FNO': None}
    near = spikes.band_check(bands, 'UP', 95, -1, 112.5)          # upper band 114
    assert (near['band_status'], near['band_takes']) == ('fixed', False)
    assert near['band_room_pct'] == pytest.approx(1.333, abs=1e-3)
    far = spikes.band_check(bands, 'UP', 100, -1, 112.5)          # upper band 120
    assert far['band_takes'] and far['band_room_pct'] == pytest.approx(6.667, abs=1e-3)
    buy = spikes.band_check(bands, 'UP', 100, 1, 82.0)            # lower band 80: 2.4%
    assert buy['band_limit'] == 80 and not buy['band_takes']
    assert spikes.band_check(bands, 'FNO', None, -1, 50) == {'band_status': 'no band',
                                                               'band_takes': True}
    for args in ((bands, 'GONE', 100), (None, 'UP', 100), (bands, 'UP', None)):
        assert spikes.band_check(*args, -1, 112.5) == {'band_status': 'unknown',
                                                       'band_takes': False}


def test_slippage_on_the_entry_and_a_time_exit_not_on_the_target():
    target = spikes.result(-1, 112.5, 107.838, 'target')
    time_exit = spikes.result(-1, 112.5, 107.838, 'time')
    assert target['quantity'] == 890 and target['net_inr'] > time_exit['net_inr']
    gap = 107.838 * spikes.SLIP * 890
    assert target['net_inr'] - time_exit['net_inr'] == pytest.approx(gap, abs=1)


def test_day_ranges_reads_the_live_session():
    def answer(request):
        assert request.url.path == '/v3/market-quote/ohlc'
        assert request.url.params['interval'] == '1d'
        return httpx.Response(200, json={'status': 'success', 'data': {
            'NSE_EQ:UP': {'last_price': 113, 'instrument_token': 'NSE_EQ|INE0UP',
                          'prev_ohlc': None, 'live_ohlc': {'open': 100, 'high': 113.8,
                                                           'low': 99.6, 'close': 113,
                                                           'volume': 5, 'ts': 1}},
            'NSE_EQ:NONE': {'last_price': 0, 'instrument_token': 'NSE_EQ|INE0NO',
                            'live_ohlc': None}}})
    feed = Upstox('t', transport=httpx.MockTransport(answer))
    assert feed.day_ranges(['NSE_EQ|INE0UP', 'NSE_EQ|INE0NO']) == {
        'NSE_EQ|INE0UP': (113.8, 99.6)}


class Feed:
    """Upstox for the scans: the day high and low of every stock, and each one's day of
    candles (the scan drops those not yet closed)."""

    def __init__(self, days, ranges):
        self.days, self.ranges, self.asked = days, ranges, []

    def day_ranges(self, keys):
        return {k: self.ranges[k] for k in keys if k in self.ranges}

    def candles(self, key):
        self.asked.append(key)
        return self.days[key]

    def close(self):
        pass


STOCKS = {'UP': ('INE0UP', 100), 'NEAR': ('INE0NEAR', 95), 'FNO': ('INE0FNO', 200),
          'QUIET': ('INE0QT', 100), 'THIN': ('INE0TH', 100)}


def _seed(conn):
    conn.execute("insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,"
                 "size_bytes,blob_path,origin) values('f','s',now(),%s,1,'p','http')",
                 ('0' * 64,))
    days = [d for d in (DAY - dt.timedelta(days=n) for n in range(1, 40))
            if d.weekday() < 5][:20]
    for symbol, (isin, close) in STOCKS.items():
        value = 1e6 if symbol == 'THIN' else 5e7
        for d in days:
            conn.execute("""insert into price_eod(exchange, trade_date, isin, symbol, series,
                                close, turnover_inr, source_fetch_id)
                            values ('NSE', %s, %s, %s, 'EQ', %s, %s, 'f')""",
                         (d, isin, symbol, close, value))
    conn.commit()


@pytest.mark.db
def test_a_day_of_scans(db_conn, monkeypatch):
    _seed(db_conn)
    monkeypatch.setattr(spikes.price_bands, 'bands',
                        lambda **_: {'UP': 20, 'NEAR': 20, 'FNO': None, 'QUIET': 20})
    up, fno = rise(), fall()
    key = {s: f'NSE_EQ|{isin}' for s, (isin, _) in STOCKS.items()}
    feed = Feed({key['UP']: up, key['NEAR']: up, key['FNO']: fno},
                {key['UP']: (113.8, 99.6), key['NEAR']: (113.8, 99.6),
                 key['FNO']: (200.5, 174.8), key['QUIET']: (101, 99), key['THIN']: (130, 100)})
    alerts, paper = [], []

    def scan(h, m):
        return spikes.step(db_conn, feed, clock=lambda: at(h, m), pause=lambda _: None,
                           alert=lambda text: alerts.append(text) or True,
                           paper=lambda text: paper.append(text) or True)

    assert scan(9, 10) == 'Spikes: outside the session'
    assert scan(10, 32) == 'Spikes: 4 quoted, 3 checked, 3 alerted, 0 paper entries sent'
    assert key['QUIET'] not in feed.asked and key['THIN'] not in feed.asked
    assert len(alerts) == 1
    assert alerts[0].startswith('SPIKE ALERT (information, not a call): 3 stocks moved 12% '
                                'or more within 30 minutes.')
    assert 'UP up 13.1%: ₹99.90 → ₹113.00 by 10:30.' in alerts[0]
    assert 'FNO down 12.5%: ₹200.10 → ₹175.00 by 10:30.' in alerts[0]
    scan(10, 42)
    assert len(alerts) == 1                                  # once a stock a day
    assert len(paper) == 1 and paper[0].startswith('Spike reversal (paper):')
    entries = paper[0].splitlines()
    assert entries[1:] == [
        'BUY FNO at ₹175.40 (10:35 open), after a 12.5% fall. Target ₹182.69; out at 15:15. '
        'No fixed circuit (a stock with futures).',
        'SHORT UP at ₹112.50 (10:35 open), after a 13.1% rise. Target ₹107.84; out at 15:15. '
        'Upper circuit ₹120.00, 6.7% above.',
        # No short sale near the upper circuit: listed after the trades taken.
        'Not taken: NEAR, a short sale at ₹112.50 after a 13.1% rise; its upper circuit '
        '₹114.00 is only 1.3% above.']
    scan(14, 0)
    assert len(paper) == 1                                   # the results wait for 15:20
    scan(15, 21)
    assert len(paper) == 2
    lines = paper[1].splitlines()
    assert lines[0] == 'Spike reversal (paper), 28 Oct: 2 trades.'
    assert lines[1].startswith('BUY FNO: out at 15:15, ₹+1,')
    assert lines[2].startswith('SHORT UP: target, ₹+3,')
    assert lines[3].startswith('Not taken, near a circuit: NEAR (short sale) would have made '
                               '₹+3,')
    rec = spikes.record(db_conn)
    assert (rec['all']['trades'], rec['band']['trades'], rec['left_out']['trades']) == (3, 2, 1)
    assert rec['all']['days'] == 1 and rec['verdict'] == 'too early: 2 of 100 trades'
    assert lines[4].startswith('Paper track: ₹+') and '(2 trades)' in lines[4]
    assert lines[5].startswith('Every spike, for comparison: ₹+') and '(3 trades)' in lines[5]
    asked = len(feed.asked)
    scan(15, 26)
    assert len(paper) == 2 and len(feed.asked) == asked      # all done: nothing to fetch
    statuses = dict(db_conn.execute('select symbol, status from intraday_spike').fetchall())
    assert statuses == {'UP': 'closed', 'NEAR': 'closed', 'FNO': 'closed'}


@pytest.mark.db
def test_an_alert_found_late_is_not_sent(db_conn, monkeypatch):
    _seed(db_conn)
    monkeypatch.setattr(spikes.price_bands, 'bands', lambda **_: {'UP': 20})
    key = 'NSE_EQ|INE0UP'
    feed = Feed({key: rise()}, {key: (113.8, 99.6)})
    alerts = []
    spikes.step(db_conn, feed, clock=lambda: at(11, 5), pause=lambda _: None,
                alert=lambda text: alerts.append(text) or True, paper=lambda text: True)
    assert alerts == []
    assert db_conn.execute('select alert_status from intraday_spike').fetchone() == ('late',)


@pytest.mark.db
def test_the_alert_threshold_is_a_setting(db_conn, monkeypatch):
    from igs.config import load_alerts
    _seed(db_conn)
    monkeypatch.setattr(spikes.price_bands, 'bands', lambda **_: {'UP': 20})
    cfg = load_alerts()
    assert spikes.rule(cfg) == {'enabled': True, 'min_move_pct': 12, 'min_value_cr': 2}
    off = cfg.model_copy(update={'rules': {**cfg.rules, 'intraday_spikes': {'enabled': False}}})
    key = 'NSE_EQ|INE0UP'
    feed = Feed({key: rise()}, {key: (113.8, 99.6)})
    alerts = []
    spikes.step(db_conn, feed, clock=lambda: at(10, 32), pause=lambda _: None, cfg=off,
                alert=lambda text: alerts.append(text) or True, paper=lambda text: True)
    assert alerts == []                                      # off: the paper track still runs
    assert db_conn.execute('select status from intraday_spike').fetchone() == ('waiting',)


def test_spike_alerts_have_their_own_chat(monkeypatch):
    from igs.alerts import delivery
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake-token')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '987654321')
    sent = []

    def answer(request):
        sent.append(dict(httpx.QueryParams(request.content.decode()))['chat_id'])
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
    client = httpx.Client(transport=httpx.MockTransport(answer))
    delivery.send_spikes('a spike', client)                  # no spikes chat: the main one
    monkeypatch.setenv('IGS_TELEGRAM_SPIKES_CHAT_ID', '-1005')
    delivery.send_spikes('a spike', client)
    assert sent == ['987654321', '-1005']


@pytest.mark.db
def test_the_tab_shows_the_records(db_conn, monkeypatch):
    import os
    from pathlib import Path

    from streamlit.testing.v1 import AppTest
    test_a_day_of_scans(db_conn, monkeypatch)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    app = Path(__file__).resolve().parents[1] / 'src' / 'igs' / 'ui' / 'app.py'
    at = AppTest.from_file(str(app), default_timeout=60).run()
    at.sidebar.radio(key='page').set_value('Paper tracking').run()
    at.session_state['paper_tab'] = 'Spike reversal'
    at.run()
    assert not at.exception, at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics['Paper track: trades'] == '2'
    assert metrics['Every spike (comparison): trades'] == '3'
    assert any('Not taken near a circuit: 1 trade(s)' in c.value for c in at.caption)
    trades = at.dataframe[-1].value                          # pandas
    assert sorted(trades['stock']) == ['FNO', 'NEAR', 'UP']
    assert dict(zip(trades['stock'], trades['paper track'], strict=True)) == {
        'FNO': 'taken', 'NEAR': 'not taken: near a circuit', 'UP': 'taken'}
