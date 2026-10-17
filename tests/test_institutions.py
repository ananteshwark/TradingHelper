"""Prominent institutions' bulk and block deals on Telegram (igs.institutions)."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
from test_intraday import seed_stock

from igs import institutions as inst
from igs.timeutil import IST

NOW = dt.datetime(2026, 10, 16, 18, 0, tzinfo=IST)
HEAD = ('Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,'
        'Trade Price / Wght. Avg. Price,Remarks\n')
BULK = HEAD + (
    '16-OCT-2026,TEST,Test Industries Ltd,GOLDMAN SACHS (SINGAPORE) PTE - ODI,BUY,100000,10.00,-\n'
    '16-OCT-2026,TEST,Test Industries Ltd,GOLDMAN SACHS BANK EUROPE SE - ODI,BUY,50000,12.00,-\n'
    '16-OCT-2026,TEST,Test Industries Ltd,GOLDMAN SACHS (SINGAPORE) PTE - ODI,SELL,30000,11.00,-\n'
    '16-OCT-2026,TEST,Test Industries Ltd,SBI MUTUAL FUND,SELL,40000,11.50,-\n'
    '16-OCT-2026,TEST,Test Industries Ltd,ORION STOCKS LTD,BUY,70889,24.91,-\n'
    '16-OCT-2026,TEST,Test Industries Ltd,HDFC MUTUAL FUND,BUY,5000,11.00,-\n'
    '16-OCT-2026,TEST,Test Industries Ltd,HDFC MUTUAL FUND,SELL,5000,11.20,-\n'
    '01-OCT-2026,TEST,Test Industries Ltd,NORGES BANK,BUY,1000,9.00,-\n')
TABLE = {'FII': {'buy': {
    'all': {'n': 772, 'd0': {'mean': 0.015}, 'd1': {'mean': 0.002},
            'd21': {'mean': 0.013, 'median': -0.004, 't': 1.2}},
    '5x and more': {'n': 37, 'd0': {'mean': 0.047}, 'd1': {'mean': 0.013},
                    'd21': {'mean': 0.061, 'median': 0.008, 't': 2.3}},
    '1-5x': {'n': 12, 'd0': {'mean': 0.02}, 'd1': {'mean': 0.0},
             'd21': {'mean': 0.0, 'median': 0.0, 't': 0.0}}}},
    'DII': {'sell': {'all': {'n': 5}}}}


def test_the_register_is_valid_and_specific_names_come_first():
    reg = inst.register()
    assert len(reg) > 100 and len({i.name for i in reg}) == len(reg)
    assert {i.type for i in reg} == {'FII', 'DII'}
    cases = {
        'GOLDMAN SACHS (SINGAPORE) PTE - ODI': ('Goldman Sachs', 'FII'),
        'HSBC MUTUAL FUND': ('HSBC Mutual Fund', 'DII'),
        'HSBC GLOBAL INVESTMENT FUNDS INDIAN EQUITY': ('HSBC Global Asset Management', 'FII'),
        'CANARA HSBC LIFE INSURANCE COMPANY LIMITED': ('Canara HSBC Life Insurance', 'DII'),
        'BARODA BNP PARIBAS MUTUAL FUND': ('Baroda BNP Paribas Mutual Fund', 'DII'),
        'BNP PARIBAS ARBITRAGE': ('BNP Paribas', 'FII'),
        'FRANKLIN TEMPLETON INVESTMENT FUNDS - FRANKLIN INDIA FUND':
            ('Franklin Templeton (foreign funds)', 'FII'),
        'KOTAKMAHINDRALIFEINSURANCECOMPANYLIMITED': ('Kotak Mahindra Life Insurance', 'DII'),
        'NIPPON LIFE INDIA TRUSTEE LTD-A/C NIPPON INDIA SMALL CAP FUND':
            ('Nippon India Mutual Fund', 'DII'),
        'THE MTBJ LTD. AS TRST FOR GOVRNMNT PENSION INVSTMNT FUND MTBJ400045828':
            ("Japan's Government Pension Investment Fund", 'FII'),
        'LIFE INSURANCE CORPORATION OF INDIA': ('LIC', 'DII'),
        'GENERAL INSURANCE CORPORATION OF INDIA': ('GIC Re', 'DII'),
        'GOVERNMENT OF SINGAPORE': ('GIC (Government of Singapore)', 'FII'),
        'BAJAJ FINSERV MUTUAL FUND': ('Other mutual fund', 'DII'),
    }
    for client, (name, kind) in cases.items():
        found = inst.identify(client, reg)
        assert found is not None and (found.name, found.type) == (name, kind), client
    # Trading houses and high-frequency firms are not institutional investors; a bare
    # "Employees Provident Fund" is ambiguous (usually Malaysia's), so it is not guessed.
    for client in ('ORION STOCKS LTD', 'GRAVITON RESEARCH CAPITAL LLP', 'JUNOMONEY FINSOL',
                   'EMPLOYEES PROVIDENT FUND', 'NAMED FUND'):
        assert inst.identify(client, reg) is None, client


def test_a_bad_register_entry_is_refused(tmp_path):
    bad = tmp_path / 'institutions.yaml'
    bad.write_text('institutions:\n  - {name: X, type: FPI, group: Y, match: [X]}\n')
    with pytest.raises(ValueError, match='bad entry'):
        inst.register(bad)


def test_trades_are_netted_per_institution_at_the_net_sides_price():
    deals = {(d['institution'].name, d['side']): d for d in inst.parse(BULK, NOW)}
    gs = deals[('Goldman Sachs', 'buy')]
    # Two client names: bought 150,000 (at 10 and 12), sold 30,000: net 120,000 bought, at
    # the buys' volume-weighted price.
    assert gs['quantity'] == Decimal(120000)
    assert gs['price'] == Decimal(1600000) / Decimal(150000)
    assert gs['clients'] == ('GOLDMAN SACHS (SINGAPORE) PTE - ODI; '
                             'GOLDMAN SACHS BANK EUROPE SE - ODI')
    assert deals[('SBI Mutual Fund', 'sell')]['value'] == Decimal(460000)
    # HDFC's buys and sells cancel out; the trading house isn't listed; Norges Bank's
    # trade is more than seven days old.
    assert set(deals) == {('Goldman Sachs', 'buy'), ('SBI Mutual Fund', 'sell')}
    assert inst.parse(HEAD + 'NO RECORDS,,,,,,,\n', NOW) == []
    with pytest.raises(ValueError):
        inst.parse(HEAD + '16-OCT-2026,TEST,Test,SBI MUTUAL FUND,HOLD,1,1,-\n', NOW)


def test_the_impact_line_quotes_the_matching_history():
    big = inst.impact('FII', 'buy', 6.0, TABLE)
    assert 'after 37 block deals in which FIIs bought 5 or more days' in big
    assert '+4.7% on the deal day' in big and '+6.1% over 21 sessions' in big
    assert 'kept beating the market, though the cases are few' in big
    assert 'history, not a forecast' in big
    # A size group with too few deals falls back to all sizes.
    mid = inst.impact('FII', 'buy', 2.0, TABLE)
    assert 'after 772 block deals in which FIIs bought (any size)' in mid
    assert 'no reliable further move' in mid
    assert inst.impact('FII', 'buy', None, TABLE).startswith(
        'Possible impact (history, not a forecast): after 772')
    assert inst.impact('DII', 'sell', 1.0, TABLE) == (
        'Possible impact: too few past cases to say (5 block deals in which DIIs sold, '
        '2013–2026).')
    # The shipped figures exist for every type and side.
    shipped = inst.impact_table()
    assert {k: set(v) for k, v in shipped.items() if k != '_about'} == {
        'FII': {'buy', 'sell'}, 'DII': {'buy', 'sell'}}


def test_the_rule_is_on_as_shipped():
    assert inst.rule() == {'enabled': True, 'types': ['FII', 'DII'], 'min_value_cr': 0}


def test_the_collection_runs_every_15_minutes_on_weekdays():
    from pathlib import Path
    timer = (Path(__file__).resolve().parents[1] / 'scripts' / 'systemd' /
             'igs-intraday-deals.timer').read_text()
    assert 'OnCalendar=Mon..Fri *-*-* 08..20:00/15:00 Asia/Kolkata' in timer


def _market(conn, cid):
    """A shareholding filing (10,00,000 shares), 20 sessions of prices with ₹1 crore traded
    a day, the deal day's close and the Nifty 500."""
    conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')""")
    fid = conn.execute("""insert into filing(company_id,exchange,filing_system,filing_type,
        period_end,filed_at,ingested_at,content_sha256,source_fetch_id,source_url)
        values(%s,'NSE','test','shareholding','2026-09-30','2026-10-10',now(),
        repeat('b',64),'f','https://example.invalid/shp') returning filing_id""",
                       (cid,)).fetchone()[0]
    conn.execute("""insert into shareholding(filing_id,company_id,period_end,category,shares,
        filed_at,ingested_at) values(%s,%s,'2026-09-30','total',1000000,'2026-10-10',now())""",
                 (fid, cid))
    day, n, closes = dt.date(2026, 9, 10), 0, []
    while day <= dt.date(2026, 10, 19):
        if day.weekday() < 5:
            close = 10.0 + 0.1 * n
            closes.append((day, close))
            conn.execute("""insert into price_eod(exchange,trade_date,isin,symbol,series,open,
                high,low,close,prev_close,volume,turnover_inr,source_fetch_id)
                values('NSE',%s,'INE123456789','TEST','EQ',%s,%s,%s,%s,%s,1000000,
                10000000,'f')""", (day, close, close, close, close, close - 0.1))
            conn.execute("""insert into index_price(index_name,trade_date,close,
                is_total_return,source_fetch_id) values('Nifty 500',%s,%s,false,'f')""",
                         (day, 20000.0))
            n += 1
        day += dt.timedelta(days=1)
    conn.commit()
    return dict(closes)


@pytest.mark.db
def test_a_new_deal_is_kept_and_sent_once_with_amount_share_price_and_impact(db_conn):
    cid = seed_stock(db_conn)
    closes = _market(db_conn, cid)
    assert inst.record(db_conn, 'bulk', BULK, 'https://example.invalid/bulk.csv', NOW) == 2
    assert inst.record(db_conn, 'bulk', BULK, 'https://example.invalid/bulk.csv', NOW) == 0
    db_conn.commit()
    gs = db_conn.execute("""select company_id, investor_type, investor_group, side,
        quantity::float8, round(pct_of_shares, 2)::float8, adv_inr::float8
        from institution_deal where institution='Goldman Sachs'""").fetchone()
    assert gs == (cid, 'FII', 'Foreign bank or broker', 'buy', 120000.0, 12.0, 10000000.0)
    # The deals count in the daily ingestion summary like any other load.
    assert db_conn.execute("select sum((payload->>'rows')::int) from operational_notification "
                           "where payload->>'table'='institution_deal'").fetchone()[0] == 2

    sent = []
    assert inst.alert(db_conn, lambda text: sent.append(text) or True, table=TABLE, now=NOW) == 2
    gs_text = next(t for t in sent if 'Goldman Sachs' in t)
    lines = gs_text.splitlines()
    assert lines[0] == 'FII BOUGHT: Test Industries Ltd (TEST)'
    assert lines[1] == ('Goldman Sachs (Foreign bank or broker: its desks often trade for '
                        'clients or arbitrage)')
    assert lines[2] == ('NSE bulk deal on 16 Oct 2026: bought 1,20,000 shares at ₹10.67 = '
                        '₹0.1 crore')
    assert lines[3] == ("12.00% of the company's shares; 0.1× its average daily trading "
                        "(20 sessions)")
    day_close = closes[dt.date(2026, 10, 16)]
    assert lines[4] == (f'On the day the stock closed '
                        f'{(day_close / (day_close - 0.1) - 1) * 100:+.1f}% at ₹{day_close:,.2f}.')
    assert 'Possible impact' in gs_text and 'Client name: GOLDMAN SACHS' in gs_text
    sbi = next(t for t in sent if 'SBI Mutual Fund' in t)
    assert sbi.startswith('DII SOLD: Test Industries Ltd (TEST)')
    assert 'too few past cases' in sbi
    assert db_conn.execute('select count(*) from institution_deal where alerted_at is null '
                           'or alert_text is null').fetchone()[0] == 0
    assert inst.alert(db_conn, lambda text: sent.append(text) or True, table=TABLE, now=NOW) == 0

    # The record: the move against the Nifty 500 from the deal day's close.
    deal = db_conn.execute("select * from institution_deal where institution='Goldman Sachs'")
    row = dict(zip([c.name for c in deal.description], deal.fetchone(), strict=True))
    nxt = closes[dt.date(2026, 10, 19)]
    assert inst.outcomes(db_conn, row) == {'next session': round((nxt / day_close - 1) * 100, 2)}


@pytest.mark.db
def test_an_unsent_deal_waits_for_telegram_and_the_rule_can_turn_alerts_off(db_conn):
    from igs.config import load_alerts
    seed_stock(db_conn)
    inst.record(db_conn, 'block', BULK, 'https://example.invalid/block.csv', NOW)
    db_conn.commit()
    assert inst.alert(db_conn, lambda text: False, table=TABLE, now=NOW) == 0      # not configured
    assert db_conn.execute('select count(*) from institution_deal where alerted_at is null'
                           ).fetchone()[0] == 2
    cfg = load_alerts()
    off = cfg.model_copy(update={'rules': {**cfg.rules, 'institution_deals': {'enabled': False}}})
    assert inst.alert(db_conn, lambda text: True, cfg=off, table=TABLE, now=NOW) == 0
    fii = cfg.model_copy(update={'rules': {**cfg.rules, 'institution_deals': {'types': ['FII']}}})
    sent = []
    assert inst.alert(db_conn, lambda text: sent.append(text) or True, cfg=fii, table=TABLE,
                      now=NOW) == 1
    assert sent[0].startswith('FII BOUGHT')
    # A later deal by the same institution in the same stock mentions the earlier one.
    later = BULK.replace('16-OCT-2026', '17-OCT-2026')
    inst.record(db_conn, 'block', later, 'https://example.invalid/block.csv',
                NOW + dt.timedelta(days=1))
    db_conn.commit()
    assert inst.alert(db_conn, lambda text: sent.append(text) or True, cfg=fii, table=TABLE,
                      now=NOW + dt.timedelta(days=1)) == 1
    # A deal first seen more than three days ago is no longer sent.
    assert inst.alert(db_conn, lambda text: True, table=TABLE,
                      now=NOW + dt.timedelta(days=5)) == 0
    assert 'Earlier here since collection began: bought ₹0.1 crore on 16 Oct 2026.' in sent[-1]


@pytest.mark.db
def test_collection_keeps_institution_deals_without_changing_the_intraday_context(db_conn):
    from igs.intraday.context import evidence
    from igs.intraday.deals import collect

    cid = seed_stock(db_conn)
    with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=BULK))) as client:
        collect(db_conn, client=client, now=NOW)
    assert db_conn.execute('select kind, institution from institution_deal order by 1, 2'
                           ).fetchall() == [('block', 'Goldman Sachs'),
                                            ('block', 'SBI Mutual Fund'),
                                            ('bulk', 'Goldman Sachs'), ('bulk', 'SBI Mutual Fund')]
    # The intraday scan still treats every name as unclassified until an admin classifies
    # it on Settings > Investors.
    assert all(e['direction'] == 0 for e in evidence(db_conn, cid, 'TEST', NOW))
    # The same trades in both files are sent once each.
    sent = []
    assert inst.alert(db_conn, lambda text: sent.append(text) or True, table=TABLE,
                      now=NOW) == 2
    assert db_conn.execute("select count(*) from institution_deal where alert_text like "
                           "'Not sent: the same trade as the bulk deal%'").fetchone()[0] == 2


@pytest.mark.db
def test_the_page_shows_deals_and_the_record(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest

    cid = seed_stock(db_conn)
    _market(db_conn, cid)
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.institutions import page
page(connect(autocommit=True))''', default_timeout=30)
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at.run()
    assert not at.exception and 'None yet' in at.info[0].value
    inst.record(db_conn, 'bulk', BULK, 'https://example.invalid/bulk.csv', NOW)
    db_conn.commit()
    at.run()
    assert not at.exception, at.exception
    summary, deals = at.dataframe[0].value, at.dataframe[1].value
    assert list(summary['type']) == ['DII', 'FII'] and list(summary['side']) == ['sell', 'buy']
    assert len(deals) == 2 and '% of shares' in deals.columns
