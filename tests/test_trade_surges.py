"""Large trading as it happens (igs.intraday.surges, docs/TRADE_SURGES.md)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from igs.intraday import surges
from igs.intraday.upstox import Upstox
from igs.timeutil import IST

DAY = dt.date(2026, 11, 26)                      # a Thursday
NEXT = dt.date(2026, 11, 27)
R = dict(surges.DEFAULTS)


def at(h, m, day=DAY):
    return dt.datetime.combine(day, dt.time(h, m, 20), IST)


def test_quotes_reads_the_full_market_quote():
    def answer(request):
        assert request.url.path == '/v2/market-quote/quotes'
        assert request.url.params['instrument_key'] == 'NSE_EQ|INE848E01016,NSE_EQ|INE0NO'
        return httpx.Response(200, json={'status': 'success', 'data': {
            # Upstox's own example (its documentation's NHPC quote), cut to what is read.
            'NSE_EQ:NHPC': {'instrument_token': 'NSE_EQ|INE848E01016', 'symbol': 'NHPC',
                            'last_price': 49.95, 'volume': 56890934, 'average_price': 50.06,
                            'total_buy_quantity': 1702906, 'total_sell_quantity': 3215393,
                            'last_trade_time': '1697624972130',
                            'timestamp': '2023-10-19T05:21:51.099+05:30'},
            'NSE_EQ:NONE': {'instrument_token': 'NSE_EQ|INE0NO', 'last_price': 10,
                            'volume': 0}}})
    feed = Upstox('t', transport=httpx.MockTransport(answer))
    assert feed.quotes(['NSE_EQ|INE848E01016', 'NSE_EQ|INE0NO']) == {
        'NSE_EQ|INE848E01016': {'volume': 56890934, 'last_price': 49.95,
                                'average_price': 50.06, 'bid_qty': 1702906.0,
                                'offer_qty': 3215393.0}}


def test_a_window_is_a_surge_only_past_both_thresholds():
    before = {'taken_at': at(10, 0), 'volume': 100000, 'last_price': 100.0}

    def quote(volume, price=102.0):
        return {'volume': volume, 'last_price': price, 'average_price': 101.0,
                'bid_qty': 4000.0, 'offer_qty': 1000.0}
    assert R['min_value_cr'] == 50 and R['min_share_of_day'] == 0.10
    w = surges.window(before, quote(6100000), at(10, 5), 2.5e9, R)  # Rs 60.6 of a 250 cr day
    assert (w['volume'], round(w['value_inr']), round(w['share_of_day'], 4)) == (
        6000000, 606000000, 0.2424)
    assert round(w['move_pct'], 2) == 2.0
    assert surges.window(before, quote(6100000), at(10, 5), 5e10, R) is None   # 1% of a day
    assert surges.window(before, quote(4100000), at(10, 5), 1e9, R) is None    # Rs 40 cr only
    assert surges.window(before, quote(6100000), at(10, 20), 2.5e9, R) is None  # 20 minutes
    assert surges.window(before, quote(90000), at(10, 5), 2.5e9, R) is None    # volume fell
    assert surges.window(None, quote(6100000), at(10, 5), 2.5e9, R) is None    # first scan


class Feed:
    """Upstox's quotes at each scan: {instrument key: (volume, price)}."""

    def __init__(self):
        self.now = {}

    def quotes(self, keys):
        return {k: {'volume': v, 'last_price': p, 'average_price': p, 'bid_qty': 3000.0,
                    'offer_qty': 1000.0} for k, (v, p) in self.now.items() if k in keys}

    def close(self):
        pass


STOCKS = {'BIG': ('INE0BIG01010', 2.5e9), 'HUGE': ('INE0HUGE1010', 5e10),
          'SMALL': ('INE0SMAL1010', 5e8)}
KEY = {s: f'NSE_EQ|{isin}' for s, (isin, _) in STOCKS.items()}


def _seed(conn):
    conn.execute("insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,"
                 "size_bytes,blob_path,origin) values('f','s',now(),%s,1,'p','http')",
                 ('0' * 64,))
    days = [d for d in (DAY - dt.timedelta(days=n) for n in range(1, 40))
            if d.weekday() < 5][:20]
    for symbol, (isin, value) in STOCKS.items():
        cid = conn.execute("insert into company (name) values (%s) returning company_id",
                           (f'{symbol} Ltd',)).fetchone()[0]
        sid = conn.execute("insert into security (company_id) values (%s) returning "
                           "security_id", (cid,)).fetchone()[0]
        conn.execute("""insert into security_identifier (security_id, id_type, id_value,
            valid_from, evidence) values (%s, 'ISIN', %s, '2020-01-01', 'test')""",
                     (sid, isin))
        for d in days:
            conn.execute("""insert into price_eod(exchange, trade_date, isin, symbol, series,
                                close, prev_close, turnover_inr, source_fetch_id)
                            values ('NSE', %s, %s, %s, 'EQ', 100, 100, %s, 'f')""",
                         (d, isin, symbol, value))
    conn.commit()


@pytest.mark.db
def test_a_day_of_scans(db_conn):
    _seed(db_conn)
    feed, sent = Feed(), []

    def scan(h, m, notify=None, day=DAY):
        return surges.step(db_conn, feed, clock=lambda: at(h, m, day), pause=lambda _: None,
                           notify=notify or (lambda text: sent.append(text) or True))

    assert scan(9, 10) == 'Surges: outside the session; 0 followed'
    feed.now = {KEY['BIG']: (100000, 100.0), KEY['HUGE']: (1000000, 100.0),
                KEY['SMALL']: (10000, 100.0)}
    assert scan(10, 0) == 'Surges: 3 quoted, 0 found, 0 alerted, 0 followed'  # the first
    feed.now = {KEY['BIG']: (6100000, 102.0),          # Rs 60.6 crore, 24% of its day
                KEY['HUGE']: (11000000, 100.5),        # Rs 100 crore, 2% of its day
                KEY['SMALL']: (4010000, 103.0)}        # 80% of its day, but Rs 40 crore
    assert scan(10, 5) == 'Surges: 3 quoted, 1 found, 1 alerted, 0 followed'
    assert sent[0].splitlines() == [
        'LARGE TRADING NOW (information, not a call): 1 stock traded a large part of a '
        'normal day in a few minutes.',
        'BIG: ₹60.6 crore traded 10:00–10:05, 24% of a normal day\'s value; price up 2.0% '
        '(₹100.00 → ₹102.00); bids 3.0× offers in the order book.',
        'Who traded is not known yet: NSE names large traders only in its block deal file '
        'after each block window and its bulk deal file after the close. The Institutional '
        'deals page keeps what followed each surge.']
    feed.now[KEY['BIG']] = (12100000, 99.0)            # another, the same stock: recorded
    assert scan(10, 10) == 'Surges: 3 quoted, 1 found, 0 alerted, 0 followed'
    assert len(sent) == 1
    statuses = db_conn.execute("""select alert_status from trade_surge
                                  order by window_to""").fetchall()
    assert statuses == [('sent',), ('repeat',)]
    assert db_conn.execute("""select company_id is not null from trade_surge
                              limit 1""").fetchone() == (True,)
    # The close and the next session come in: each surge is followed against the Nifty 500.
    for day, close, nifty in ((DAY, 104.0, 20000.0), (NEXT, 106.08, 20100.0)):
        db_conn.execute("""insert into price_eod(exchange, trade_date, isin, symbol, series,
            close, prev_close, turnover_inr, source_fetch_id)
            values ('NSE', %s, 'INE0BIG01010', 'BIG', 'EQ', %s, %s, 2.5e9, 'f')""",
                        (day, close, 100.0 if day == DAY else 104.0))
        db_conn.execute("""insert into index_price(index_name, trade_date, close,
            is_total_return, source_fetch_id) values ('Nifty 500', %s, %s, false, 'f')""",
                        (day, nifty))
    db_conn.commit()
    assert scan(9, 10, day=NEXT + dt.timedelta(days=3)).endswith('2 followed')
    got = db_conn.execute("""select round(to_close_pct, 3)::float8,
        round(next_vs_index_pct, 2)::float8 from trade_surge order by window_to""").fetchall()
    assert got == [(1.961, 1.5), (5.051, 1.5)]          # +2% next session, Nifty +0.5%
    rec = surges.record(db_conn)
    # Signed for each window's move: the first rose (+1.96% to the close), the second fell
    # and then rose (-5.05%); the next session +1.5% and -1.5%.
    assert (rec['surges'], rec['with_close'], rec['named']) == (2, 2, 0)
    assert round(rec['to_close_pct'], 3) == round((1.961 - 5.051) / 2, 3)
    assert round(rec['next_vs_index_pct'], 3) == 0.0


@pytest.mark.db
def test_a_message_waits_while_telegram_is_down_but_not_long(db_conn):
    _seed(db_conn)
    feed = Feed()
    feed.now = {KEY['BIG']: (100000, 100.0)}

    def scan(h, m, ok):
        return surges.step(db_conn, feed, clock=lambda: at(h, m), pause=lambda _: None,
                           notify=lambda text: ok)
    scan(11, 0, True)
    feed.now = {KEY['BIG']: (6100000, 102.0)}
    assert scan(11, 5, False).endswith('1 found, 0 alerted, 0 followed')
    assert scan(11, 10, True).endswith('0 found, 1 alerted, 0 followed')   # retried
    feed.now = {KEY['BIG']: (12600000, 104.0)}
    scan(11, 15, False)
    feed.now = {KEY['BIG']: (12600001, 104.0)}
    assert scan(11, 40, True).endswith('0 alerted, 0 followed')            # a repeat anyway


@pytest.mark.db
def test_the_intraday_job_runs_it_and_the_page_shows_it(db_conn, monkeypatch):
    import inspect
    import os

    from streamlit.testing.v1 import AppTest

    from igs import cli
    assert '_surges_step(conn)' in inspect.getsource(cli._intraday)
    _seed(db_conn)
    feed = Feed()
    feed.now = {KEY['BIG']: (100000, 100.0)}
    surges.step(db_conn, feed, clock=lambda: at(12, 0), pause=lambda _: None,
                notify=lambda text: True)
    feed.now = {KEY['BIG']: (6100000, 98.0)}
    surges.step(db_conn, feed, clock=lambda: at(12, 5), pause=lambda _: None,
                notify=lambda text: True)
    db_conn.execute("""insert into institution_deal (kind, trade_date, symbol, institution,
            investor_type, investor_group, client_names, side, quantity, price, value_inr,
            source_url)
        values ('bulk', %s, 'BIG', 'Example Mutual Fund', 'DII', 'Mutual fund',
                'EXAMPLE MF', 'sell', 1000000, 98, 98000000, 'https://example.test')""",
                    (DAY,))
    db_conn.commit()
    assert surges.record(db_conn)['named'] == 1
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at_ = AppTest.from_string('''from igs.db import connect
from igs.ui.surges import section
section(connect(autocommit=True))''', default_timeout=30)
    at_.run()
    assert not at_.exception, at_.exception
    table = at_.dataframe[0].value
    assert list(table['stock']) == ['BIG']
    assert list(table['named that day']) == ['Example Mutual Fund (bulk, sell)']
    assert list(table['price move %']) == [-2.0]
