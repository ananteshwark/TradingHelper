"""The week, month and year price chart after each call or ranking message on Telegram
(igs.alerts.price_chart, delivery.send_chart)."""
from __future__ import annotations

import datetime as dt
import io

import httpx
import pytest
from test_intraday import seed_stock

from igs.alerts import delivery, price_chart
from igs.timeutil import IST

DAY = dt.date(2026, 10, 16)


def _days(n=260, start=100.0):
    out, d, p = [], DAY, start
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d, p))
            p *= 0.999
        d -= dt.timedelta(days=1)
    return out[::-1]


def test_the_chart_is_one_image_of_three_panels():
    from PIL import Image
    png = price_chart.render('TEST', 'Test Industries Ltd', _days())
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert Image.open(io.BytesIO(png)).size == (price_chart.WIDTH_PX, price_chart.HEIGHT_PX)
    # With 5-minute prices for the week, and with too few closes to draw anything.
    week = [(dt.datetime(2026, 10, 12 + k // 75, 9, 15, tzinfo=IST)
             + dt.timedelta(minutes=5 * (k % 75)), 100.0 + k / 100) for k in range(375)]
    assert price_chart.render('TEST', None, _days(), week)[:4] == b'\x89PNG'
    assert price_chart.render('TEST', None, _days(1)) is None


def test_rupees_and_changes_read_the_indian_way():
    assert price_chart._rupees(1234567.4) == '₹12,34,567'
    assert price_chart._rupees(85.5) == '₹85.50'
    assert price_chart._change([(1, 100.0), (2, 90.0)]) == '▼ -10.0%   ₹100 → ₹90.00'
    assert price_chart._change([(1, 1000.0), (2, 1100.0)]) == '▲ +10.0%   ₹1,000 → ₹1,100'


@pytest.mark.db
def test_closes_are_adjusted_for_a_split_and_the_week_uses_5_minute_prices(db_conn):
    cid = seed_stock(db_conn)
    db_conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')""")
    # 200, 202, then a 2-for-1 split: the exchange's previous close becomes 101.
    for day, close, prev in ((dt.date(2026, 10, 13), 200, 198), (dt.date(2026, 10, 14), 202, 200),
                             (dt.date(2026, 10, 15), 102, 101), (DAY, 103, 102)):
        db_conn.execute("""insert into price_eod(exchange,trade_date,isin,symbol,series,open,
            high,low,close,prev_close,volume,source_fetch_id)
            values('NSE',%s,'INE123456789','TEST','EQ',%s,%s,%s,%s,%s,100,'f')""",
                        (day, close, close, close, close, prev))
    db_conn.commit()
    days = price_chart.daily(db_conn, cid, DAY)
    assert [d for d, _ in days] == [dt.date(2026, 10, 13), dt.date(2026, 10, 14),
                                    dt.date(2026, 10, 15), DAY]
    assert [round(p, 2) for _, p in days] == [100.0, 101.0, 102.0, 103.0]
    assert price_chart.company_of(db_conn, 'TEST') == cid and price_chart.company_of(
        db_conn, 'NOPE') is None
    # The intraday scan's candles count only with three of the last five sessions.
    from psycopg.types.json import Jsonb

    def candles(day):
        start = dt.datetime.combine(day, dt.time(9, 15), IST)
        return Jsonb([{'start': (start + dt.timedelta(minutes=5 * k)).isoformat(),
                       'open': 1, 'high': 1, 'low': 1, 'close': 100 + k, 'volume': 1}
                      for k in range(3)])
    for day in (dt.date(2026, 10, 14), DAY):
        db_conn.execute("insert into intraday_history(instrument_key,session_date,candles) "
                        "values('NSE_EQ|INE123456789',%s,%s)", (day, candles(day)))
    assert price_chart.intraday(db_conn, cid, DAY) == []
    db_conn.execute("insert into intraday_history(instrument_key,session_date,candles) "
                    "values('NSE_EQ|INE123456789',%s,%s)",
                    (dt.date(2026, 10, 15), candles(dt.date(2026, 10, 15))))
    week = price_chart.intraday(db_conn, cid, DAY)
    assert len(week) == 9 and week[0][0].date() == dt.date(2026, 10, 14)
    png, caption = price_chart.chart(db_conn, cid, DAY)
    assert png[:4] == b'\x89PNG' and caption == 'TEST: price over the last week, month and year'
    other = db_conn.execute("insert into company(name) values('No prices') returning company_id"
                            ).fetchone()[0]
    assert price_chart.chart(db_conn, other, DAY) is None


def test_the_photo_goes_to_the_chat_as_a_reply(monkeypatch):
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '123')
    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 2}})
    client = httpx.Client(transport=httpx.MockTransport(receive))
    assert delivery.send_telegram_photo(b'\x89PNG...', 'TEST: chart', client, reply_to=7)
    body = seen[0].content
    assert seen[0].url.path.endswith('/sendPhoto')
    assert b'name="chat_id"\r\n\r\n123' in body and b'name="reply_to_message_id"\r\n\r\n7' in body
    assert b'name="caption"\r\n\r\nTEST: chart' in body and b'filename="chart.png"' in body
    monkeypatch.delenv('IGS_TELEGRAM_TOKEN')
    assert delivery.send_telegram_photo(b'png', 'x', client) is False


@pytest.mark.db
def test_a_chart_never_holds_up_the_message_it_follows(db_conn, monkeypatch):
    cid = seed_stock(db_conn)
    monkeypatch.setenv('IGS_TELEGRAM_TOKEN', 'fake')
    monkeypatch.setenv('IGS_TELEGRAM_CHAT_ID', '123')
    sent = []
    monkeypatch.setattr(delivery, 'send_telegram_photo', lambda *a: sent.append(a) or True)
    monkeypatch.setattr(price_chart, 'chart', lambda conn, company_id: (b'png', 'caption'))
    assert delivery.send_chart(db_conn, cid, reply_to=5)
    assert sent == [(b'png', 'caption', None, 5, 'calls')]       # to the calls' chat
    # Off in config/alerts.yaml, no company, or a chart that fails: skipped, no error.
    monkeypatch.setattr(delivery, 'charts_on', lambda: False)
    assert delivery.send_chart(db_conn, cid) is False
    monkeypatch.setattr(delivery, 'charts_on', lambda: True)
    assert delivery.send_chart(db_conn, None) is False

    def broken(conn, company_id):
        conn.execute('select * from no_such_table')
    monkeypatch.setattr(price_chart, 'chart', broken)
    assert delivery.send_chart(db_conn, cid) is False
    assert db_conn.execute('select 1').fetchone() == (1,)     # the caller's work goes on
    assert len(sent) == 1


def test_charts_are_on_as_shipped():
    from igs.config import load_alerts
    assert load_alerts().call_messages.charts is True
