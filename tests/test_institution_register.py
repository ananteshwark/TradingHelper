"""Who counts as an FII or a DII, refreshed from SEBI's registers (igs.institution_register),
and their deals and takeover-code filings on Telegram (igs.institutions)."""
from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs

import httpx
import pytest
from test_intraday import seed_stock

from igs import institution_register as reg
from igs import institutions as inst
from igs.guardrails import find_advice_language
from igs.timeutil import IST

NOW = dt.datetime(2026, 11, 22, 5, 17, tzinfo=IST)


def _card(name, reg_no, country=None, quote='"'):
    q = quote
    fields = [('Name', name), ('Registration No.', reg_no), ('Address', 'Somewhere'),
              (' Validity ', 'Jul 08, 2016 - Jul 10, 2028')]
    if country:
        fields.append(('Country Name', country))
    cards = ''.join(f"<div class={q}card-view{q}><div class={q}title{q}><span>{k}</span></div>"
                    f"<div class={q}value varun-text{q}><span>{v}</span></div></div>"
                    for k, v in fields)
    return (f"<div class={q}fixed-table-body card-table{q}>"
            f"<div class={q}card-table-left{q}>{cards}</div></div>")


def _page(records, start, total, quote='"'):
    """One of SEBI's register pages: the first as the page serves it (double quotes), later
    ones as its AJAX call does (single quotes)."""
    body = ''.join(_card(*r, quote=quote) for r in records)
    return (f"<div id='ajax_cat'><p>{start} to {start + len(records) - 1} of {total} "
            f"records</p>{body}</div>")


def _sebi(pages: dict[int, list], total: int, seen: list | None = None,
          refuse: int | None = None):
    """A transport serving SEBI's register: page 0 by GET, the others by POST."""
    def handler(request: httpx.Request):
        if request.method == 'GET':
            return httpx.Response(200, text=_page(pages[0], 1, total))
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        n = int(form['doDirect'])
        if seen is not None:
            seen.append((n, request.headers.get('referer', '')))
        if n == refuse:
            return httpx.Response(200, text='<TITLE>Unauthorized Request Blocked</TITLE>')
        return httpx.Response(200, text=_page(pages.get(n, []), n * 2 + 1, total, "'"))
    return httpx.Client(transport=httpx.MockTransport(handler))


FPIS = {0: [('1199 SEIU Greater New York Pension Fund', 'INUSFP170516', 'UNITED STATES'),
            ('Goldman Sachs (Singapore) Pte.', 'INSGFP000001', 'SINGAPORE')],
        1: [('Jane Street Singapore Pte. Ltd.', 'INSGFP000002', 'SINGAPORE'),
            ('Kuwait Investment Office', 'INKWFP000003', 'KUWAIT')],
        2: [('The Master Trust Bank of Japan, Ltd.', 'INJPFP000004', 'JAPAN')]}
FUNDS = {0: [('Abakkus Mutual Fund', 'MF/088/25/14'), ('LIC MF', 'MF/002/94/2')]}


def test_names_match_without_punctuation_suffixes_or_odi():
    assert reg.norm('Goldman Sachs (Singapore) Pte.') == \
        reg.norm('GOLDMAN SACHS (SINGAPORE) PTE - ODI') == 'GOLDMANSACHSSINGAPORE'
    assert reg.norm('The Master Trust Bank of Japan, Ltd.') == 'MASTERTRUSTBANKOFJAPAN'
    assert reg.norm('LIC MF') == 'LICMUTUALFUND'
    assert reg.norm('B & K') == ''                        # too short to match safely


def test_both_page_layouts_are_read():
    first, total = reg.parse_page(_page(FPIS[0], 1, 5))
    later, _ = reg.parse_page(_page(FPIS[1], 3, 5, "'"))
    assert total == 5
    assert [r['name'] for r in first + later] == [
        '1199 SEIU Greater New York Pension Fund', 'Goldman Sachs (Singapore) Pte.',
        'Jane Street Singapore Pte. Ltd.', 'Kuwait Investment Office']
    assert first[0] == {'name': '1199 SEIU Greater New York Pension Fund',
                        'reg_no': 'INUSFP170516', 'country': 'UNITED STATES',
                        'validity': 'Jul 08, 2016 - Jul 10, 2028'}


def test_every_page_is_read_as_the_site_asks_for_it():
    seen = []
    records = reg.fetch('sebi_fpi', client=_sebi(FPIS, 5, seen), pause=lambda s: None)
    assert len(records) == 5
    assert [n for n, _ in seen] == [1, 2]
    assert all(ref.endswith('intmId=29') for _, ref in seen)       # the page's own referer
    with pytest.raises(reg.RegisterError, match='refused page 2'):
        reg.fetch('sebi_fpi', client=_sebi(FPIS, 5, refuse=1), pause=lambda s: None)
    with pytest.raises(reg.RegisterError, match='incomplete: 5 of 9'):
        reg.fetch('sebi_fpi', client=_sebi(FPIS, 9), pause=lambda s: None)


def test_the_summary_says_what_changed():
    assert reg.summary({'sebi_fpi': {'total': 5, 'added': ['A'] * 5, 'removed': [],
                                     'first': True}}).splitlines()[1] == \
        'Foreign portfolio investors: 5 loaded.'
    text = reg.summary({'sebi_fpi': {'total': 6, 'added': ['New Fund'], 'removed': ['Old'],
                                     'first': False},
                        'sebi_mf': {'error': 'SEBI refused page 2'}})
    assert 'Foreign portfolio investors: 6 registered; 1 new, 1 no longer listed.' in text
    assert 'New: New Fund.' in text and 'No longer listed: Old.' in text
    assert 'Mutual funds: not refreshed (SEBI refused page 2); the last list stays in use.' \
        in text
    assert reg.summary({'sebi_mf': {'total': 2, 'added': [], 'removed': [],
                                    'first': False}}) is None


@pytest.mark.db
def test_a_refresh_keeps_the_lists_and_sends_what_changed(db_conn):
    sent = []
    clients = iter([_sebi(FPIS, 5), _sebi(FUNDS, 2)])

    def refresh(**kw):
        return reg.refresh(db_conn, client=next(clients), pause=lambda s: None,
                           notify=lambda t: sent.append(t) or True, now=NOW, **kw)
    first = refresh(sources=('sebi_fpi',))
    assert first['sebi_fpi']['first'] and first['sebi_fpi']['total'] == 5
    refresh(sources=('sebi_mf',))
    assert len(sent) == 2 and 'Mutual funds: 2 loaded.' in sent[1]
    # A week later: one FPI gone, one new.
    later = dict(FPIS)
    later[2] = [('Norges Bank', 'INNOFP000005', 'NORWAY')]
    got = reg.refresh(db_conn, sources=('sebi_fpi',), client=_sebi(later, 5),
                      pause=lambda s: None, notify=lambda t: sent.append(t) or True,
                      now=NOW + dt.timedelta(days=7))
    assert got['sebi_fpi']['added'] == ['Norges Bank']
    assert got['sebi_fpi']['removed'] == ['The Master Trust Bank of Japan, Ltd.']
    assert 'New: Norges Bank.' in sent[-1]
    # A refused refresh leaves the list as it was.
    reg.refresh(db_conn, sources=('sebi_fpi',), client=_sebi(later, 5, refuse=1),
                pause=lambda s: None, notify=lambda t: sent.append(t) or True, now=NOW)
    assert db_conn.execute("select count(*) from institution_register where active "
                           "and source='sebi_fpi'").fetchone()[0] == 5
    assert 'not refreshed' in sent[-1]
    runs = db_conn.execute('select status from institution_register_run order by run_id'
                           ).fetchall()
    assert [s for (s,) in runs] == ['ok', 'ok', 'ok', 'failed']
    status = {r['list']: r['registered'] for r in reg.status(db_conn)}
    assert status == {'foreign portfolio investors': 5, 'mutual funds': 2}


def _loaded(conn):
    reg.store(conn, 'sebi_fpi', [r for p in FPIS.values() for r in
                                 [{'name': n, 'reg_no': k, 'country': c, 'validity': None}
                                  for n, k, c in p]], NOW)
    reg.store(conn, 'sebi_mf', [{'name': n, 'reg_no': k, 'country': None, 'validity': None}
                                for n, k in FUNDS[0]], NOW)
    conn.commit()


@pytest.mark.db
def test_the_register_finds_fpis_and_funds_but_not_hft_traders(db_conn):
    _loaded(db_conn)
    find = reg.lookup(db_conn)
    assert find('KUWAIT INVESTMENT OFFICE').name == 'Kuwait Investment Office'
    assert find('KUWAIT INVESTMENT OFFICE').type == 'FII'
    fund = find('ABAKKUS MUTUAL FUND A/C ABAKKUS FLEXI CAP FUND')
    assert (fund.name, fund.type, fund.group) == ('Abakkus Mutual Fund', 'DII', 'Mutual fund')
    assert find('JANE STREET SINGAPORE PTE LTD') is None          # excluded: a market maker
    assert find('KUWAIT INVESTMENT SECURITIES') is None            # not the same name


HEAD = ('Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,'
        'Trade Price / Wght. Avg. Price,Remarks\n')
DEALS = HEAD + (
    '20-NOV-2026,TEST,Test Industries Ltd,KUWAIT INVESTMENT OFFICE,BUY,1000000,100.00,-\n'
    '20-NOV-2026,TEST,Test Industries Ltd,1199 SEIU GREATER NEW YORK PENSION FUND,BUY,1000,'
    '100.00,-\n'
    '20-NOV-2026,TEST,Test Industries Ltd,JANE STREET SINGAPORE PTE LTD,SELL,900000,100.00,-\n'
    '20-NOV-2026,TEST,Test Industries Ltd,GOLDMAN SACHS (SINGAPORE) PTE - ODI,BUY,500,100.00,-\n')


@pytest.mark.db
def test_register_deals_are_kept_and_the_small_ones_left_unsent(db_conn):
    cid = seed_stock(db_conn)
    _loaded(db_conn)
    now = dt.datetime(2026, 11, 20, 18, 0, tzinfo=IST)
    assert inst.record(db_conn, 'bulk', DEALS, 'https://example.invalid/bulk.csv', now) == 3
    db_conn.commit()
    rows = dict(db_conn.execute('select institution, from_register from institution_deal'
                                ).fetchall())
    # The hand list wins for Goldman Sachs; Jane Street is never kept.
    assert rows == {'Kuwait Investment Office': True,
                    '1199 SEIU Greater New York Pension Fund': True, 'Goldman Sachs': False}
    sent = []
    assert inst.alert(db_conn, lambda t: sent.append(t) or True, now=now) == 2
    # ₹10 crore by a register-only FPI is sent; ₹1 lakh by another is under ₹5 crore.
    kio = next(t for t in sent if 'Kuwait' in t)
    assert kio.splitlines()[:2] == [
        'FII BOUGHT: Test Industries Ltd (TEST)',
        "Kuwait Investment Office (Foreign portfolio investor, on SEBI's register)"]
    assert any(t.startswith('FII BOUGHT') and 'Goldman Sachs' in t for t in sent)
    assert not any('SEIU' in t for t in sent)
    assert not any(find_advice_language(t) for t in sent)
    assert cid


def _announce(conn, cid, subject, n=0, category='Disclosure under SEBI Takeover Regulations'):
    conn.execute("""insert into raw_payload(fetch_id,source_id,fetched_at,content_sha256,
        size_bytes,blob_path,origin) values ('f','test',now(),repeat('a',64),0,'x','manual')
        on conflict do nothing""")
    conn.execute("""insert into announcement (exchange, symbol, company_id, filed_at, category,
            subject, attachment_url, source_fetch_id, ingested_at)
        values ('NSE', 'TEST', %s, %s, %s, %s, %s, 'f', %s)""",
                 (cid, NOW - dt.timedelta(minutes=n), category, subject,
                  f'https://nsearchives.nseindia.com/corporate/sast{n}.pdf', NOW))
    conn.commit()


@pytest.mark.db
def test_takeover_code_filings_by_institutions_are_sent(db_conn):
    cid = seed_stock(db_conn)
    _loaded(db_conn)
    name = ('Disclosure under Regulation 29(2) of SEBI (Substantial Acquisition of Shares & '
            'Takeovers) Regulations, 2011')
    _announce(db_conn, cid, f'Nomura Asset Management Co Ltd has submitted to the Exchange '
                            f'a copy of {name} for sale of shares', 0)
    _announce(db_conn, cid, f'Kuwait Investment Office has submitted to the Exchange a '
                            f'copy of {name}', 1)
    _announce(db_conn, cid, 'Nomura Asset Management Co Ltd has submitted a disclosure under '
                            'Regulation 31 of SEBI (SAST) Regulations, 2011', 2)   # a pledge
    _announce(db_conn, cid, f'Mr. Some Person has submitted to the Exchange a copy of {name}',
              3)                                                       # not an institution
    _announce(db_conn, cid, 'Analyst meet', 4, category='Updates')
    assert inst.record_filings(db_conn, NOW) == 2
    assert inst.record_filings(db_conn, NOW) == 0
    sent = []
    assert inst.alert_filings(db_conn, lambda t: sent.append(t) or True, now=NOW) == 2
    nomura = next(t for t in sent if 'Nomura' in t)
    assert nomura.splitlines()[:2] == [
        'FII FILING (takeover code): Test (TEST)',
        "Nomura (Foreign bank or broker) filed under SEBI's takeover code (Regulation 29) on "
        "22 Nov 2026 05:17 IST:"]
    assert 'It reports shares disposed of; the figures are in the filing.' in nomura
    kio = next(t for t in sent if 'Kuwait' in t)
    assert "Kuwait Investment Office (Foreign portfolio investor) filed" in kio
    assert 'a holding of 5% or more, or a change of 2% or more' in kio
    assert kio.endswith('https://nsearchives.nseindia.com/corporate/sast1.pdf')
    assert not any(find_advice_language(t) for t in sent)
    assert inst.filings_step(db_conn, notify=lambda t: True, now=NOW) == \
        '0 institution filings found, 0 sent'


def test_the_refresh_runs_every_sunday_and_the_data_check_sends_filings():
    import inspect
    from pathlib import Path

    from igs import sync
    units = Path(__file__).resolve().parents[1] / 'scripts' / 'systemd'
    timer = (units / 'igs-institutions.timer').read_text()
    assert 'OnCalendar=Sun *-*-* 05:17 Asia/Kolkata' in timer and 'Persistent=true' in timer
    assert 'igs-job.sh institutions refresh' in (units / 'igs-institutions.service').read_text()
    assert 'institutions.filings_step(ctx.conn)' in inspect.getsource(sync.ingest_steps)


@pytest.mark.db
def test_the_page_shows_the_filings_and_who_counts(db_conn, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('IGS_DATABASE_URL', os.environ['IGS_TEST_DATABASE_URL'])
    cid = seed_stock(db_conn)
    _loaded(db_conn)
    _announce(db_conn, cid, 'Kuwait Investment Office has submitted to the Exchange a copy '
                            'of Disclosure under Regulation 29(1) of SEBI (SAST) Regulations')
    inst.record_filings(db_conn, NOW)
    at = AppTest.from_string('''from igs.db import connect
from igs.ui.institutions import page
page(connect(autocommit=True))''', default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    filings, counts = at.dataframe[0].value, at.dataframe[1].value
    assert list(filings['institution']) == ['Kuwait Investment Office']
    assert dict(zip(counts['list'], counts['registered'], strict=True)) == {
        'foreign portfolio investors': 5, 'mutual funds': 2}
