"""The traders whose same-day bets in NSE's bulk deal files keep winning (igs.deal_traders)."""
from __future__ import annotations

import datetime as dt

import pytest
from test_intraday import seed_stock

from igs import deal_traders as dtr
from igs.guardrails import find_advice_language
from igs.timeutil import IST

HEAD = ('Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,'
        'Trade Price / Wght. Avg. Price,Remarks\n')


def _days(n, start=dt.date(2026, 10, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _file(day, rows):
    return HEAD + ''.join(f"{day:%d-%b-%Y},TEST,Test Industries Ltd,{c},{s},{q},{p},-\n"
                          for c, s, q, p in rows)


def _collect(conn, day, rows, n=0, kind='bulk'):
    conn.execute("""insert into intraday_deal_fetch (content_hash, fetched_at, source_url,
        body) values (%s, %s, %s, %s)""",
                 (f'{kind}{day}{n}', dt.datetime.combine(day, dt.time(18, n), IST),
                  f'https://nsearchives.nseindia.com/content/equities/{kind}.csv',
                  _file(day, rows)))
    conn.commit()


def test_luck_is_the_chance_of_as_many_wins_from_coin_tosses():
    assert dtr.p_value(20, 20) == 1 / 2 ** 20
    assert dtr.p_value(0, 10) == 1.0
    assert round(dtr.p_value(15, 20), 4) == 0.0207


def test_a_bet_is_what_a_client_bought_and_sold_the_same_day():
    day = dt.date(2026, 10, 1)
    got = {b['client']: b for b in dtr.bets(_file(day, [
        ('QUICK TRADER', 'BUY', 1000, '100.00'), ('QUICK TRADER', 'SELL', 600, '101.00'),
        ('QUICK TRADER', 'SELL', 400, '102.00'), ('LONG FUND', 'BUY', 5000, '100.50')]), day)}
    q = got['QUICK TRADER']
    assert (q['matched_qty'], q['net_qty'], float(q['sell_price'])) == (1000, 0, 101.4)
    assert float(q['pnl_inr']) == 1400.0 and round(float(q['ret_pct']), 2) == 1.4
    lf = got['LONG FUND']
    assert lf['matched_qty'] == 0 and lf['pnl_inr'] is None and lf['net_qty'] == 5000


@pytest.mark.db
def test_consistent_winners_are_found_named_once_and_described(db_conn):
    seed_stock(db_conn)
    for n, day in enumerate(_days(25)):
        rows = [('QUICK TRADER', 'BUY', 100000, '100.00'),
                ('QUICK TRADER', 'SELL', 100000, '100.20'),
                ('JANE STREET SINGAPORE PTE LTD', 'BUY', 50000, '100.00'),
                ('JANE STREET SINGAPORE PTE LTD', 'SELL', 50000, '100.05'),
                ('COIN TOSSER', 'BUY', 1000, '100.00'),
                ('COIN TOSSER', 'SELL', 1000, '101.00' if n % 2 else '99.00'),
                ('GOLDMAN SACHS (SINGAPORE) PTE - ODI', 'BUY', 9000, '100.00')]
        _collect(db_conn, day, rows)
    # A later copy of the last day's file replaces that day's figures, once.
    last = _days(25)[-1]
    _collect(db_conn, last, [('QUICK TRADER', 'BUY', 100000, '100.00'),
                             ('QUICK TRADER', 'SELL', 100000, '100.30')], n=5)
    assert dtr.record(db_conn) == 26
    assert dtr.record(db_conn) == 0
    everyone = {t['client']: t for t in dtr.traders(db_conn)}
    quick = everyone['QUICK TRADER']
    assert (quick['bets'], quick['wins'], quick['stocks']) == (25, 25, 1)
    assert round(quick['pnl_inr']) == 24 * 20000 + 30000
    assert quick['what'] == 'other: a broker, proprietary trader or individual'
    assert everyone['JANE STREET SINGAPORE PTE LTD']['what'] == \
        'high-frequency or arbitrage trader'
    assert everyone['COIN TOSSER']['win_rate'] == 12 / 25
    assert 'GOLDMAN SACHS (SINGAPORE) PTE - ODI' not in everyone       # no same-day bet
    rule = dtr.rule()
    assert [t['client'] for t in everyone.values() if dtr.consistent(t, rule)] == \
        ['QUICK TRADER', 'JANE STREET SINGAPORE PTE LTD']
    sent = []
    assert dtr.alert(db_conn, notify=lambda t: sent.append(t) or True) == 2
    assert dtr.alert(db_conn, notify=lambda t: sent.append(t) or True) == 0
    text = sent[0]
    assert text.startswith("WINNING TRADERS in NSE's bulk deal files")
    assert ('QUICK TRADER (other: a broker, proprietary trader or individual)\nSince '
            '01 Oct 2026: 25 same-day round trips in 1 stocks, 25 won (100%)') in text
    assert "can't be copied the same day" in text
    assert not find_advice_language(text)


@pytest.mark.db
def test_what_was_carried_is_marked_at_the_close_and_followed(db_conn):
    seed_stock(db_conn)
    db_conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')""")
    days = _days(3, dt.date(2026, 11, 16))
    for day, close, prev, nifty in zip(days, (100.0, 103.0, 104.0), (100.0, 100.0, 103.0),
                                       (20000.0, 20100.0, 20200.0), strict=True):
        db_conn.execute("""insert into price_eod(exchange,trade_date,isin,symbol,series,open,
            high,low,close,prev_close,volume,turnover_inr,source_fetch_id)
            values('NSE',%s,'INE123456789','TEST','EQ',%s,%s,%s,%s,%s,1000000,1e8,'f')""",
                        (day, close, close, close, close, prev))
        db_conn.execute("""insert into index_price(index_name,trade_date,close,
            is_total_return,source_fetch_id) values('Nifty 500',%s,%s,false,'f')""",
                        (day, nifty))
    _collect(db_conn, days[0], [('QUICK TRADER', 'BUY', 3000, '99.00'),
                                ('QUICK TRADER', 'SELL', 1000, '99.50')])
    dtr.record(db_conn)
    now = dt.datetime.combine(days[-1], dt.time(20, 0), IST)
    assert dtr.follow(db_conn, now) == 1
    close, net_pnl, nxt = db_conn.execute("""select close::float8, net_pnl_inr::float8,
        next_vs_index_pct::float8 from deal_trader_bet""").fetchone()
    assert (close, net_pnl) == (100.0, 2000.0)          # 2,000 carried, bought at ₹99
    assert nxt == pytest.approx(3.0 - 0.5, abs=0.01)    # +3% against the Nifty 500's +0.5%
    t = dtr.traders(db_conn)[0]
    assert (t['carried'], round(t['carried_next_pct'], 2)) == (1, 2.5)


@pytest.mark.db
def test_the_deals_job_runs_it_and_the_page_lists_them(db_conn, monkeypatch):
    import inspect
    import os

    from streamlit.testing.v1 import AppTest

    from igs import cli
    assert 'deal_traders.step(conn)' in inspect.getsource(cli._intraday_deals)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.traders import page
page(connect(autocommit=True))''', default_timeout=30)
    at.run()
    assert not at.exception and 'None yet' in at.info[0].value
    seed_stock(db_conn)
    for day in _days(20):
        _collect(db_conn, day, [('QUICK TRADER', 'BUY', 100, '10.00'),
                                ('QUICK TRADER', 'SELL', 100, '10.10')])
    assert dtr.step(db_conn, notify=lambda t: True).startswith(
        'Deal traders: 20 files read, 0 followed, 1 newly flagged')
    at.run()
    assert not at.exception, at.exception
    table = at.dataframe[0].value
    assert list(table['client']) == ['QUICK TRADER']
    assert list(table['consistent winner']) == [True]
