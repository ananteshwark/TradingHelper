"""Promoters' own buying and selling, in every company, on Telegram (igs.promoter_trades)."""
from __future__ import annotations

import datetime as dt

import pytest
from test_intraday import seed_stock

from igs import promoter_trades as pt
from igs.guardrails import find_advice_language
from igs.timeutil import IST

NOW = dt.datetime(2026, 11, 18, 18, 0, tzinfo=IST)


def test_a_takeover_filings_side_is_read_past_the_regulations_name():
    name = ('Disclosure under Regulation 29(2) of SEBI (Substantial Acquisition of Shares & '
            'Takeovers) Regulations, 2011')
    assert pt._side_in_text(f'X has submitted a copy of {name}') == 'unknown'
    assert pt._side_in_text(f'{name} for acquisition of shares') == 'buy'
    assert pt._side_in_text(f'{name} for sale of shares') == 'sell'


def test_names_match_whatever_the_spelling():
    assert pt.norm_name('Mr. Rakesh Chopdar') == pt.norm_name('RAKESH CHOPDAR') == \
        'RAKESHCHOPDAR'
    assert pt.norm_name('Blue Diamond Properties Pvt. Ltd.') == \
        pt.norm_name('BLUE DIAMOND PROPERTIES PRIVATE LIMITED') == 'BLUEDIAMONDPROPERTIES'
    assert pt.norm_name('Smt Shobhana Mittal') == 'SHOBHANAMITTAL'
    assert pt.norm_name('A B') == ''                     # too short to match safely


def test_only_trades_through_the_market_count():
    assert pt.through_market('Market Sale', True)
    assert pt.through_market('Block Deal', False)        # a mode the app doesn't know: kept
    assert pt.through_market('Open Offer', False)
    for mode in ('Inter-se-Transfer', 'Gift', 'ESOP', 'Off Market', 'Pledge Creation', '-',
                 None, 'Off-Market Transfer', 'Inter se transfer of shares', 'Transmission',
                 'Buyback'):
        assert not pt.through_market(mode, False), mode
    assert pt.is_promoter('Member of the Promoter Group', 'other')
    assert pt.is_promoter('Promoters') and not pt.is_promoter('Director', 'director_kmp')


def _fetch(conn):
    conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')
        on conflict do nothing""")


def _insider(conn, symbol, person, category, side, qty, value, mode='Market Purchase',
             open_market=True, role='promoter', filed=NOW, day=dt.date(2026, 11, 16),
             before=None, after=None, security='Equity Shares', ingested=None):
    _fetch(conn)
    conn.execute("""insert into insider_trade (exchange, symbol, company_name, person_name,
            person_category, insider_role, regulation, security_type, transaction_type,
            acquisition_mode, side, open_market, quantity, value_inr, holding_before_pct,
            holding_after_pct, trade_from, trade_to, intimated_on, filed_at, xbrl_url,
            source_fetch_id, ingested_at, submission_type)
        values ('NSE', %s, null, %s, %s, %s, '7(2)', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, 'https://nsearchives.example/pit.xml', 'f', %s, 'Original')""",
                 (symbol, person, category, role, security, side.title(), mode, side,
                  open_market, qty, value, before, after, day, day, day, filed,
                  ingested or NOW - dt.timedelta(minutes=5)))
    conn.commit()


def _stocks(conn):
    green = seed_stock(conn, 'GREENLAM', 'INE544R01013')
    azad = seed_stock(conn, 'AZAD', 'INE02IJ01035')
    conn.execute("update company set name='Greenlam Industries Ltd' where company_id=%s",
                 (green,))
    conn.execute("update company set name='Azad Engineering Ltd' where company_id=%s", (azad,))
    conn.commit()
    return green, azad


@pytest.mark.db
def test_promoters_market_trades_in_disclosures_are_sent_once(db_conn):
    green, _ = _stocks(db_conn)
    _insider(db_conn, 'GREENLAM', 'Blue Diamond Properties Pvt Ltd', 'Promoter Group', 'buy',
             1022000, 214000000, before=14.89, after=15.30)
    # A member of the promoter group the normaliser filed as 'other'.
    _insider(db_conn, 'GREENLAM', 'Parul Mittal', 'Member of the Promoter Group', 'sell',
             50000, 10500000, mode='Market Sale', role='other')
    # Not trades through the market, or not by a promoter.
    _insider(db_conn, 'GREENLAM', 'Saurabh Mittal', 'Promoter', 'buy', 5611000, None,
             mode='Inter-se-Transfer', open_market=False)
    _insider(db_conn, 'GREENLAM', 'A Director', 'Director', 'buy', 1000, 200000,
             role='director_kmp')
    _insider(db_conn, 'GREENLAM', 'Blue Diamond Properties Pvt Ltd', 'Promoter Group', 'buy',
             1000, 210000, security='Warrants')
    assert pt.record_disclosures(db_conn, NOW) == 2
    assert pt.record_disclosures(db_conn, NOW) == 0
    sent = []
    assert pt.alert(db_conn, notify=lambda t: sent.append(t) or True, now=NOW) == 2
    buy = next(m for m in sent if 'BOUGHT' in m)
    assert buy.startswith('PROMOTER BOUGHT: Greenlam Industries Ltd (GREENLAM)')
    assert ('Blue Diamond Properties Pvt Ltd (Promoter Group) bought 10,22,000 shares through '
            'the market (Market Purchase) for ₹21.40 crore') in buy
    assert 'Their holding: 14.89% → 15.30%.' in buy
    assert "Disclosed under SEBI's insider-trading rules on 18 Nov 2026 18:00 IST." in buy
    assert any(m.startswith('PROMOTER SOLD: Greenlam') for m in sent)
    assert not any(find_advice_language(m) for m in sent)
    assert pt.alert(db_conn, notify=lambda t: sent.append(t) or True, now=NOW) == 0
    assert db_conn.execute('select count(*) from promoter_trade where company_id=%s',
                           (green,)).fetchone()[0] == 2


@pytest.mark.db
def test_a_block_deal_by_a_known_promoter_is_sent_on_the_deal_day(db_conn):
    _, azad = _stocks(db_conn)
    # Rakesh Chopdar is known as a promoter from an earlier disclosure.
    _insider(db_conn, 'AZAD', 'Mr. Rakesh Chopdar', 'Promoter', 'sell', 100, 150000,
             mode='Market Sale', filed=NOW - dt.timedelta(days=200),
             day=dt.date(2026, 5, 1), ingested=NOW - dt.timedelta(days=200))
    head = ('Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,'
            'Trade Price / Wght. Avg. Price,Remarks\n')
    body = head + ('18-NOV-2026,AZAD,Azad Engineering Ltd,RAKESH CHOPDAR,SELL,3000000,1650.00,-\n'
                   '18-NOV-2026,AZAD,Azad Engineering Ltd,SOME FUND LTD,BUY,3000000,1650.00,-\n'
                   '18-NOV-2026,AZAD,Azad Engineering Ltd,RAKESH CHOPDAR,BUY,100000,1660.00,-\n')
    db_conn.execute("""insert into intraday_deal_fetch (content_hash, fetched_at, source_url,
        body) values ('h1', %s, 'https://nsearchives.nseindia.com/content/equities/block.csv',
        %s)""", (NOW, body))
    db_conn.commit()
    assert pt.record_deals(db_conn, NOW) == 1
    assert pt.record_deals(db_conn, NOW) == 0              # the file is checked once
    row = db_conn.execute("""select source, person, side, quantity, round(price, 2)
        from promoter_trade""").fetchone()
    assert row[:3] == ('block', 'RAKESH CHOPDAR', 'sell')
    assert (float(row[3]), float(row[4])) == (2900000.0, 1650.0)
    sent = []
    assert pt.alert(db_conn, notify=lambda t: sent.append(t) or True, now=NOW) == 1
    assert sent[0].startswith('PROMOTER SOLD (NSE block deal): Azad Engineering Ltd (AZAD)')
    assert ('RAKESH CHOPDAR (a promoter, "Mr. Rakesh Chopdar" in its insider-trading '
            'disclosures) sold 29,00,000 shares in an NSE block deal for ₹478.50 crore') \
        in sent[0]
    assert azad


@pytest.mark.db
def test_a_takeover_code_filing_naming_a_promoter_is_sent(db_conn):
    green, _ = _stocks(db_conn)
    _insider(db_conn, 'GREENLAM', 'Blue Diamond Properties Private Limited', 'Promoter Group',
             'buy', 100, 21000, filed=NOW - dt.timedelta(days=90), day=dt.date(2026, 8, 1),
             ingested=NOW - dt.timedelta(days=90))
    _fetch(db_conn)
    subjects = [
        ('Disclosure under SEBI Takeover Regulations',
         'Blue Diamond Properties Pvt Ltd has submitted to the Exchange a copy of Disclosure '
         'under Regulation 29(2) of SEBI (Substantial Acquisition of Shares & Takeovers) '
         'Regulations, 2011 for acquisition of shares'),
        ('Disclosure under SEBI Takeover Regulations',
         'Blue Diamond Properties Pvt Ltd has submitted a disclosure under Regulation 31 of '
         'SEBI (SAST) Regulations, 2011 for creation of pledge'),       # not a trade
        ('Disclosure under SEBI Takeover Regulations',
         'Some Mutual Fund has submitted to the Exchange a copy of Disclosure under '
         'Regulation 29(1) of SEBI (SAST) Regulations, 2011'),          # not a promoter
        ('Updates', 'Investor presentation')]
    for n, (category, subject) in enumerate(subjects):
        db_conn.execute("""insert into announcement (exchange, symbol, company_id, filed_at,
                category, subject, attachment_url, source_fetch_id, ingested_at)
            values ('NSE', 'GREENLAM', %s, %s, %s, %s, %s, 'f', %s)""",
                        (green, NOW - dt.timedelta(minutes=n), category, subject,
                         f'https://nsearchives.nseindia.com/corporate/sast{n}.pdf', NOW))
    db_conn.commit()
    assert pt.record_sast(db_conn, NOW) == 1
    sent = []
    pt.alert(db_conn, notify=lambda t: sent.append(t) or True, now=NOW)
    assert sent[0].startswith('PROMOTER BOUGHT: Greenlam Industries Ltd (GREENLAM)')
    assert 'naming Blue Diamond Properties Private Limited (known promoter)' in sent[0]
    assert 'The figures are in the filing' not in sent[0]   # its side is in the text
    assert sent[0].endswith('https://nsearchives.nseindia.com/corporate/sast0.pdf')


@pytest.mark.db
def test_the_rule_sets_scope_minimum_and_sides_and_telegram_failures_wait(db_conn):
    from igs.config import load_alerts
    green, azad = _stocks(db_conn)
    _insider(db_conn, 'GREENLAM', 'Small Promoter', 'Promoter', 'buy', 100, 2000000)
    _insider(db_conn, 'AZAD', 'Small Promoter Two', 'Promoter', 'sell', 100, 2000000,
             mode='Market Sale')
    db_conn.execute('insert into watchlist (company_id) values (%s)', (azad,))
    db_conn.commit()
    pt.record_disclosures(db_conn, NOW)
    cfg = load_alerts()
    rule = pt.rule(cfg)
    assert (rule['scope'], rule['min_value_cr']) == ('all', 0.5)
    # ₹20 lakh: below the minimum outside the watchlist; on it, sent.
    assert [t['symbol'] for t in pt.due(db_conn, rule, NOW)] == ['AZAD']
    assert [t['symbol'] for t in pt.due(db_conn, {**rule, 'min_value_cr': 0}, NOW)] == \
        ['GREENLAM', 'AZAD']
    assert pt.due(db_conn, {**rule, 'include_sells': False}, NOW) == []
    assert pt.due(db_conn, rule, NOW + dt.timedelta(days=4)) == []   # too old to send
    # Telegram unavailable: nothing is marked; the next run sends it.
    assert pt.alert(db_conn, notify=lambda t: False, now=NOW) == 0
    assert pt.alert(db_conn, notify=lambda t: True, now=NOW) == 1
    assert green


@pytest.mark.db
def test_the_watchlist_rule_leaves_promoters_to_this_one(db_conn):
    from igs.alerts.rules import watchlist_insider_trades
    green, _ = _stocks(db_conn)
    db_conn.execute('insert into watchlist (company_id) values (%s)', (green,))
    _insider(db_conn, 'GREENLAM', 'Blue Diamond Properties Pvt Ltd', 'Promoter Group', 'buy',
             1022000, 214000000)
    _insider(db_conn, 'GREENLAM', 'A Director', 'Director', 'buy', 1000, 200000,
             role='director_kmp')
    since, until = NOW - dt.timedelta(hours=1), NOW + dt.timedelta(hours=1)
    both = watchlist_insider_trades(db_conn, since, until, {})
    assert len(both) == 2
    only = watchlist_insider_trades(db_conn, since, until, {'skip_promoters': True})
    assert [a.message.split(': ')[2].split(' (')[0] for a in only] == ['A Director']


@pytest.mark.db
def test_the_data_check_and_the_deals_job_run_it(db_conn, monkeypatch):
    _stocks(db_conn)
    _insider(db_conn, 'GREENLAM', 'Blue Diamond Properties Pvt Ltd', 'Promoter Group', 'buy',
             1022000, 214000000)
    sent = []
    assert pt.step(db_conn, notify=lambda t: sent.append(t) or True, now=NOW) == \
        '1 promoter trades found, 1 sent'
    assert pt.step(db_conn, deals=True, notify=lambda t: True, now=NOW) == \
        '0 promoter trades found, 0 sent'
    import inspect

    from igs import cli, sync
    assert 'promoter_trades.step(ctx.conn)' in inspect.getsource(sync.ingest_steps)
    assert 'promoter_trades.step(conn, deals=True)' in inspect.getsource(cli._intraday_deals)


@pytest.mark.db
def test_the_page_lists_promoter_trades_and_their_record(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.promoters import page
page(connect(autocommit=True))''', default_timeout=30)
    at.run()
    assert not at.exception and 'None yet' in at.info[0].value
    _stocks(db_conn)
    _insider(db_conn, 'GREENLAM', 'Blue Diamond Properties Pvt Ltd', 'Promoter Group', 'buy',
             1022000, 214000000, before=14.89, after=15.30)
    pt.record_disclosures(db_conn, NOW)
    at.run()
    assert not at.exception, at.exception
    summary, trades = at.dataframe[0].value, at.dataframe[1].value
    assert list(summary['side']) == ['buy'] and list(trades['stock']) == ['GREENLAM']
    assert list(trades['source']) == ['insider disclosure']
