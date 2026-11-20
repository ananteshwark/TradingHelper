"""Following the winning traders on paper: what they carried, the next session from its open
to its close, after charges (igs.follow_winners)."""
from __future__ import annotations

import datetime as dt

import pytest
from test_deal_traders import _collect
from test_intraday import seed_stock

from igs import deal_traders as dtr
from igs import follow_winners as fw
from igs.timeutil import IST

THU = dt.date(2026, 11, 19)
FRI = dt.date(2026, 11, 20)


def _at(day, hour, minute=0):
    return dt.datetime.combine(day, dt.time(hour, minute), IST)


def _flag(conn, client, when):
    conn.execute("insert into deal_trader_flag (client, flagged_at, bets, wins) "
                 "values (%s, %s, 20, 20)", (client, when))
    conn.commit()


def _price(conn, day, o, h, low, c, series='EQ', symbol='TEST', isin='INE123456789'):
    conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')
        on conflict do nothing""")
    conn.execute("""insert into price_eod(exchange,trade_date,isin,symbol,series,open,high,low,
        close,prev_close,volume,turnover_inr,source_fetch_id)
        values('NSE',%s,%s,%s,%s,%s,%s,%s,%s,%s,1000000,1e8,'f')""",
                 (day, isin, symbol, series, o, h, low, c, o))
    conn.commit()


def _read(conn, day, rows, when):
    _collect(conn, day, rows)
    dtr.record(conn, when)


def test_the_paper_record_and_its_bar():
    rec = {'trades': 12, 'won': 7, 'net_inr': 900.0, 't': 1.1, 'passes': False}
    assert fw.bar_text(rec).startswith('none until the paper record clears the bar: 60 '
                                       'trades or more')
    assert 'real orders can be considered' in fw.bar_text({**rec, 'passes': True})


@pytest.mark.db
def test_a_winners_carried_purchase_is_bought_at_the_next_open_and_settled(db_conn):
    seed_stock(db_conn)
    _flag(db_conn, 'QUICK TRADER', _at(THU, 9))
    # Bought 60,000 and sold 10,000 at ₹100: carried ₹50 lakh.
    _read(db_conn, THU, [('QUICK TRADER', 'BUY', 60000, '100.00'),
                         ('QUICK TRADER', 'SELL', 10000, '100.50')], _at(THU, 18, 30))
    sent = []
    note = lambda t: sent.append(t) or True                     # noqa: E731
    assert fw.step(db_conn, notify=note, now=_at(THU, 18, 45)) == \
        'Following winners: 0 settled, 1 opened, 1 messages sent'
    assert sent[0].startswith('FOLLOWING WINNING TRADERS (paper): 1 trade for 20 Nov')
    assert ('BUY TEST: QUICK TRADER carried a net purchase of ₹0.50 crore at the close on '
            '19 Nov.') in sent[0]
    # Nothing more until the session's prices are in, and nothing sent twice.
    assert fw.step(db_conn, notify=note, now=_at(THU, 19)) == \
        'Following winners: 0 settled, 0 opened, 0 messages sent'
    _price(db_conn, FRI, 100.0, 104.0, 99.0, 103.0)
    assert fw.step(db_conn, notify=note, now=_at(FRI, 19)).startswith(
        'Following winners: 1 settled')
    t = db_conn.execute("""select status, entry::float8, exit::float8, quantity,
        gross_inr::float8, charges_inr::float8, net_inr::float8 from follow_trade""").fetchone()
    status, entry, exit_, qty, gross, cost, net = t
    assert (status, entry, exit_, qty) == ('closed', 100.05, 102.95, 999)  # 0.05% slippage
    assert gross == pytest.approx(2.9 * 999) and 0 < cost < 200
    assert net == pytest.approx(gross - cost)
    assert sent[1].startswith('FOLLOWING WINNING TRADERS (paper): 1 trade settled, ₹+2,')
    assert 'Real orders: none until the paper record clears the bar' in sent[1]
    rec = fw.record(db_conn)
    assert (rec['trades'], rec['won'], rec['passes']) == (1, 1, False)


@pytest.mark.db
def test_only_trades_that_could_have_been_placed_are_kept(db_conn):
    seed_stock(db_conn)
    _flag(db_conn, 'QUICK TRADER', _at(THU, 9))
    _flag(db_conn, 'OTHER WINNER', _at(THU, 9))
    _flag(db_conn, 'NAMED LATER', _at(FRI, 9))
    _read(db_conn, THU, [
        ('QUICK TRADER', 'BUY', 60000, '100.00'),
        ('OTHER WINNER', 'SELL', 60000, '100.00'),         # the other side: TEST cancels
        ('NAMED LATER', 'BUY', 90000, '100.00'),           # named after the file was read
        ('QUICK TRADER', 'BUY', 1000, '100.00'),           # (same client: adds up)
    ], _at(THU, 18, 30))
    assert fw.open_trades(db_conn, fw.rule(), _at(THU, 18, 45)) == 0
    # A file read after the next session opened is too late to follow.
    _read(db_conn, FRI, [('QUICK TRADER', 'BUY', 60000, '100.00')], _at(FRI, 18, 30))
    assert fw.open_trades(db_conn, fw.rule(), dt.datetime(2026, 11, 23, 9, 20, tzinfo=IST)) == 0
    assert fw.open_trades(db_conn, fw.rule(), _at(FRI, 18, 45)) == 1
    assert db_conn.execute('select session from follow_trade').fetchone()[0] == \
        dt.date(2026, 11, 23)                               # Friday's file: Monday


@pytest.mark.db
def test_a_locked_stock_gives_no_fill_and_trade_for_trade_none(db_conn):
    seed_stock(db_conn)
    seed_stock(db_conn, 'TTT', 'INE000000003')
    _flag(db_conn, 'QUICK TRADER', _at(THU, 9))
    _collect(db_conn, THU, [('QUICK TRADER', 'BUY', 60000, '100.00')])
    db_conn.execute("""insert into intraday_deal_fetch (content_hash, fetched_at, source_url,
        body) values ('t2t', %s, 'https://nsearchives.nseindia.com/content/equities/bulk.csv',
        %s)""", (_at(THU, 18, 1), dtr_file(THU)))
    db_conn.commit()
    dtr.record(db_conn, _at(THU, 18, 30))
    assert fw.open_trades(db_conn, fw.rule(), _at(THU, 18, 45)) == 2
    _price(db_conn, FRI, 105.0, 105.0, 101.0, 105.0)       # opened and closed at the band
    _price(db_conn, FRI, 50.0, 52.0, 49.0, 51.0, series='BE', symbol='TTT',
           isin='INE000000003')
    assert fw.settle(db_conn, fw.rule(), _at(FRI, 19)) == 2
    got = dict(db_conn.execute('select symbol, status from follow_trade').fetchall())
    assert got == {'TEST': 'no fill', 'TTT': 'not intraday'}
    assert fw.record(db_conn)['trades'] == 0


def dtr_file(day):
    from test_deal_traders import HEAD
    return HEAD + f"{day:%d-%b-%Y},TTT,TTT Ltd,QUICK TRADER,SELL,80000,50.00,-\n"


@pytest.mark.db
def test_messages_wait_while_telegram_is_unavailable_but_not_past_the_open(db_conn):
    seed_stock(db_conn)
    _flag(db_conn, 'QUICK TRADER', _at(THU, 9))
    _read(db_conn, THU, [('QUICK TRADER', 'BUY', 60000, '100.00')], _at(THU, 18, 30))
    sent = []
    note = lambda t: sent.append(t) or True                     # noqa: E731
    assert fw.step(db_conn, notify=lambda t: False, now=_at(THU, 18, 45)) == \
        'Following winners: 0 settled, 1 opened, 0 messages sent'
    assert fw.step(db_conn, notify=note, now=_at(THU, 19)).endswith('1 messages sent')
    # Friday's note, never sent before Monday's open, isn't sent after it.
    _read(db_conn, FRI, [('QUICK TRADER', 'BUY', 60000, '100.00')], _at(FRI, 18, 30))
    assert fw.step(db_conn, notify=lambda t: False, now=_at(FRI, 18, 45)).endswith(
        '1 opened, 0 messages sent')
    monday = dt.datetime(2026, 11, 23, 9, 30, tzinfo=IST)
    assert fw.step(db_conn, notify=note, now=monday) == \
        'Following winners: 0 settled, 0 opened, 0 messages sent'
    assert len(sent) == 1


@pytest.mark.db
def test_the_deals_job_runs_it_and_the_page_shows_the_record(db_conn, monkeypatch):
    import inspect
    import os

    from streamlit.testing.v1 import AppTest

    from igs import cli
    src = inspect.getsource(cli._intraday_deals)
    assert src.index('deal_traders.step(conn)') < src.index('follow_winners.step(conn)')
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    seed_stock(db_conn)
    _flag(db_conn, 'QUICK TRADER', _at(THU, 9))
    _read(db_conn, THU, [('QUICK TRADER', 'BUY', 60000, '100.00')], _at(THU, 18, 30))
    fw.step(db_conn, notify=lambda t: True, now=_at(THU, 18, 45))
    _price(db_conn, FRI, 100.0, 104.0, 99.0, 103.0)
    fw.step(db_conn, notify=lambda t: True, now=_at(FRI, 19))
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.traders import page
page(connect(autocommit=True))''', default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    assert any('Following them (paper)' in s.value for s in at.subheader)
    trades = at.dataframe[-1].value
    assert list(trades['stock']) == ['TEST'] and list(trades['side']) == ['buy']
