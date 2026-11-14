"""Spike picks (igs.intraday.spike_ml, research/spike_ml/PAPER.md): the features against the
study's own code, the exported model, the trade from candles, and a day of scans against a
made-up Upstox feed with a stand-in model. No request leaves the machine."""
from __future__ import annotations

import datetime as dt
import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest
from test_spikes import Feed, at, bars, fall, rise

from igs.intraday import spike_ml, spikes
from igs.intraday.engine import Candle

ROOT = Path(__file__).resolve().parents[1]
DAY = dt.date(2026, 10, 28)


def _study():
    spec = importlib.util.spec_from_file_location(
        'spike_ml_events', ROOT / 'research' / 'spike_ml' / 'events.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nifty():
    return bars([(20000 + i, 20005 + i, 19995 + i, 20001 + i) for i in range(75)])


def test_the_features_are_the_study_s():
    study, day = _study(), fall()
    nifty = _nifty()
    study.ARR.update(
        t=np.array([spike_ml._minutes(b) for b in day], dtype=np.int32),
        o=np.array([b.open for b in day]), h=np.array([b.high for b in day]),
        l=np.array([b.low for b in day]), c=np.array([b.close for b in day]),
        v=np.array([1000.0 + 10 * n for n in range(len(day))]),
        offsets=np.array([0, len(day)]), meta=[(DAY, 'INE0FL', False, 5e7, 201.0, 0.012)])
    study.NIFTY[DAY] = (np.array([spike_ml._minutes(b) for b in nifty], dtype=float),
                        np.array([b.open for b in nifty]), np.array([b.close for b in nifty]))
    want = study.work((0, 1))
    day = [Candle(b.start, b.open, b.high, b.low, b.close, 1000.0 + 10 * n)
           for n, b in enumerate(day)]
    got = spike_ml.points(day, 201.0, 0.012, 5e7, nifty)
    assert [(p['x'], p['i'], p['side']) for p in got] == [(w['x'], w['i'], w['side'])
                                                          for w in want]
    for p, w in zip(got, want, strict=True):
        for f in spike_ml.FEATURES[:-2]:          # the crowd's two are counted across stocks
            assert p['features'][f] == pytest.approx(w[f], abs=1e-12), f


def test_the_exported_model_loads():
    model = spike_ml.picker()
    assert model.features == spike_ml.FEATURES and model.targets == [1, 2, 3, 4, 5]
    assert model.gate == 0.5 and model.meta['trained_on'] == '2022-01-03 to 2026-10-08'
    p = spike_ml.points(fall(), 201.0, 0.0, 5e7)[0]
    preds = model.predict({**p['features'], 'f_crowd': 0, 'f_crowd_side': 0})
    assert len(preds) == 5 and all(math.isfinite(x) for x in preds)


def test_the_trade_from_candles():
    day = rise()
    decision = day[11].start                       # the 5% point: the 10:10 candle
    t = spike_ml.settle(day, decision, 1, 2, final=False)    # a short, 2% target
    assert (t['entry'], t['exit_reason']) == (106, 'target')
    assert t['exit'] == pytest.approx(106 * 0.9985 * 0.98)
    out = spike_ml.settle(fall(), fall()[11].start, -1, 3, final=False)   # a buy, 3%
    assert (out['entry'], out['exit_reason'], out['exit']) == (188, 'time', 175.4 + 0.05 * 56)
    assert spike_ml.settle(day[:12], decision, 1, 2, final=False) is None
    assert spike_ml.settle(day[:12], decision, 1, 2, final=True) == {'void': True}


class Stub:
    """A stand-in model: after a fall its best is the 3% target, after a rise the 2%."""
    gate, targets = 0.5, [1, 2, 3, 4, 5]

    def predict(self, f):
        return [0.1, 0.2, 0.9, 0.3, 0.1] if f['f_side'] == -1 else [0.1, 0.6, 0.2, 0.1, 0.0]


STOCKS = {'DROP': ('INE0DR', 201), 'POP': ('INE0PO', 100), 'NEARC': ('INE0NC', 90)}


def _seed(conn):
    conn.execute("insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,"
                 "size_bytes,blob_path,origin) values('f','s',now(),%s,1,'p','http')",
                 ('0' * 64,))
    days = [d for d in (DAY - dt.timedelta(days=n) for n in range(1, 40))
            if d.weekday() < 5][:20]
    for symbol, (isin, close) in STOCKS.items():
        for d in days:
            conn.execute("""insert into price_eod(exchange, trade_date, isin, symbol, series,
                                close, prev_close, turnover_inr, source_fetch_id)
                            values ('NSE', %s, %s, %s, 'EQ', %s, %s, 5e7, 'f')""",
                         (d, isin, symbol, close, close / 1.01))
    conn.commit()


def _day(conn, monkeypatch, first_scan):
    from igs.config import load_alerts
    _seed(conn)
    monkeypatch.setattr(spike_ml, 'picker', lambda path=None: Stub())
    monkeypatch.setattr(spikes.price_bands, 'bands',
                        lambda **_: {'DROP': 20, 'POP': 20, 'NEARC': 20})
    key = {s: f'NSE_EQ|{isin}' for s, (isin, _) in STOCKS.items()}
    feed = Feed({key['DROP']: fall(), key['POP']: rise(), key['NEARC']: rise(),
                 spike_ml.NIFTY: _nifty()},
                {key['DROP']: (200.5, 174.8, 175), key['POP']: (113.8, 99.6, 113),
                 key['NEARC']: (113.8, 99.6, 113)})
    cfg = load_alerts()
    # Only the picks' alerts: the 12% alerts are off here (test_spikes covers them).
    cfg = cfg.model_copy(update={'rules': {**cfg.rules, 'intraday_spikes': {
        'enabled': False, 'model_picks': True}}})
    alerts, paper = [], []

    def scan(h, m):
        return spikes.step(conn, feed, clock=lambda: at(h, m), pause=lambda _: None, cfg=cfg,
                           alert=lambda text: alerts.append(text) or True,
                           paper=lambda text: paper.append(text) or True)
    scan(*first_scan)
    return scan, alerts, paper


@pytest.mark.db
def test_a_day_of_picks(db_conn, monkeypatch):
    scan, alerts, paper = _day(db_conn, monkeypatch, (10, 12))
    assert alerts == []                            # the 10:10 candle closes at 10:15
    scan(10, 17)
    assert len(alerts) == 1
    lines = alerts[0].splitlines()
    assert lines[0] == 'SPIKE PICKS (the model, on paper): 2 new.'
    # The 10:10 candle's window starts at 09:45: the 09:55 high and low are in it.
    assert lines[1] == ('BUY DROP after a 6.2% fall within 30 minutes (₹200.50 → ₹188.00 by '
                        '10:15). The model expects +0.9% after costs with a 3% target (about '
                        '₹193.64); paper entry at the 10:15 open, out by 15:15.')
    assert lines[2].startswith('SHORT POP after a 6.3% rise within 30 minutes (₹99.70 → '
                               '₹106.00 by 10:15). The model expects +0.6% after costs with a '
                               '2% target')
    # No short sale near the upper circuit: NEARC's (₹108) is 1.9% above.
    assert lines[3] == ('Not taken: SHORT NEARC after a 6.3% rise; its upper circuit ₹108.00 '
                        'is only 1.9% above.')
    scan(10, 22)
    assert len(alerts) == 1                        # one pick a stock a day
    scan(15, 21)
    results = [p for p in paper if p.startswith('Spike picks (paper)')]
    assert len(results) == 1
    lines = results[0].splitlines()
    assert lines[0] == 'Spike picks (paper), 28 Oct: 2 trades.'
    assert lines[1].startswith('BUY DROP: out at 15:15, ₹-')
    assert lines[2].startswith('SHORT POP: target, ₹+')
    assert lines[3].startswith('Not taken, near the upper circuit: SHORT NEARC would have made')
    rec = spike_ml.record(db_conn)
    assert (rec['buys']['trades'], rec['shorts']['trades'], rec['all_shorts']['trades']) == (
        1, 1, 2)
    assert rec['buys']['verdict'] == 'too early: 1 of 100 trades'
    assert lines[4].startswith('Buys: ₹-') and lines[5].startswith('Short sales: ₹+')
    assert lines[6].startswith('Every short sale, for comparison: ₹+')
    row = db_conn.execute("""select entry, target_pct, exit_reason, takes, features->>'f_crowd'
                             from intraday_spike_ml where symbol = 'POP'""").fetchone()
    assert (float(row[0]), row[1], row[2], row[3]) == (106.0, 2, 'target', True)
    # The crowd counts 5% points strictly before (as the study did): all three share a candle.
    assert float(row[4]) == 0
    scan(15, 26)
    assert len([p for p in paper if p.startswith('Spike picks')]) == 1


@pytest.mark.db
def test_a_pick_found_too_late_is_missed(db_conn, monkeypatch):
    scan, alerts, paper = _day(db_conn, monkeypatch, (10, 40))
    assert alerts == []                    # found 25 minutes after its candle closed
    assert db_conn.execute("select count(*) from intraday_spike_ml where status = 'missed'"
                           ).fetchone() == (3,)
    scan(15, 21)
    lines = [p for p in paper if p.startswith('Spike picks')][0].splitlines()
    assert lines[0] == 'Spike picks (paper), 28 Oct: 0 trades.'
    assert any(x.startswith('Missed (found too late): BUY DROP would have made') for x in lines)
    rec = spike_ml.record(db_conn)
    assert rec['missed']['trades'] == 3 and rec['buys']['trades'] == 0


@pytest.mark.db
def test_the_tab_shows_both_records(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    scan, _, _ = _day(db_conn, monkeypatch, (10, 17))
    scan(15, 21)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at_ = AppTest.from_file(str(ROOT / 'src' / 'igs' / 'ui' / 'app.py'), default_timeout=60).run()
    at_.sidebar.radio(key='page').set_value('Paper tracking').run()
    at_.session_state['paper_tab'] = 'Spike picks'
    at_.run()
    assert not at_.exception, at_.exception
    metrics = {m.label: m.value for m in at_.metric}
    assert metrics['Buys after falls: trades'] == '1'
    assert metrics['Short sales after rises: trades'] == '1'
    assert metrics['Every short sale (comparison): trades'] == '2'
    picks = at_.dataframe[-1].value                          # pandas
    assert dict(zip(picks['stock'], picks['record'], strict=True)) == {
        'DROP': 'taken', 'POP': 'taken', 'NEARC': 'not taken: near the upper circuit'}


@pytest.mark.db
def test_an_earlier_day_s_pick_is_settled_from_that_day_s_candles(db_conn, monkeypatch):
    scan, alerts, _ = _day(db_conn, monkeypatch, (10, 17))
    assert len(alerts) == 1                    # picked, then the watcher stopped for the day
    later = dt.date(2026, 10, 29)

    class Next(Feed):
        def candles_between(self, key, start, end):
            assert start == end == DAY
            return self.days.get(key, [])
    key = {s: f'NSE_EQ|{isin}' for s, (isin, _) in STOCKS.items()}
    feed = Next({key['DROP']: fall(), key['POP']: rise(), key['NEARC']: rise()}, {})
    asked = spike_ml.settle_old(db_conn, feed, at(9, 20, later), lambda _: None)
    assert asked == 3
    rows = dict(db_conn.execute("select symbol, status from intraday_spike_ml").fetchall())
    assert rows == {'DROP': 'closed', 'POP': 'closed', 'NEARC': 'closed'}
    assert db_conn.execute("select exit_reason from intraday_spike_ml where symbol = 'DROP'"
                           ).fetchone() == ('time',)
