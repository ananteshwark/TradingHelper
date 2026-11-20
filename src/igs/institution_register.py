"""Who counts as an FII or a DII, refreshed every week from SEBI's own registers
(docs/INSTITUTIONAL_DEALS.md, "The register").

`config/institutions.yaml` names about 150 prominent institutions by hand, with their
groups. On top of it, `igs institutions refresh` (igs-institutions.timer, Sundays) reads
SEBI's lists of registered intermediaries, updated daily at sebi.gov.in:
- **Foreign portfolio investors** (FPIs and deemed FPIs, the erstwhile FIIs): FII;
- **Mutual funds**: DII.

Insurers and pension fund managers change rarely and stay in the hand-written list.

A deal file's client name matches an FPI when the two names are the same after
normalisation (capitals, no punctuation, no legal suffix such as "Pte Ltd" or "- ODI").
It matches a mutual fund when it contains the fund's name ("ABAKKUS MUTUAL FUND A/C ...").
`exclude` in config/institutions.yaml names the high-frequency and arbitrage traders that
are registered as FPIs but are not investors; their deals are never sent.

After each refresh, Telegram (the deals chat) gets what changed: new registrations and
the ones that left. Nothing here changes a call or an order.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from igs.timeutil import IST, utc_now

BASE = 'https://www.sebi.gov.in'
PAGE = BASE + '/sebiweb/other/OtherAction.do?doRecognisedFpi=yes&intmId={id}'
AJAX = BASE + '/sebiweb/ajax/other/getintmfpiinfo.jsp'
USER_AGENT = 'IndiaGrowthScreener/1.0 (FII and DII register refresh)'
PAUSE_S = 2.5                       # between SEBI's pages: about 20 minutes for the FPIs
COMPLETE = 0.98                     # a list with fewer records than this share is not used
SOURCES = {
    'sebi_fpi': {'intm_id': 29, 'type': 'FII', 'group': 'Foreign portfolio investor',
                 'what': 'foreign portfolio investors'},
    'sebi_mf': {'intm_id': 23, 'type': 'DII', 'group': 'Mutual fund',
                'what': 'mutual funds'},
}
SUFFIXES = {'ODI', 'PTE', 'PRIVATE', 'PVT', 'LTD', 'LIMITED', 'LLC', 'INC', 'PLC', 'SA', 'AG',
            'NV', 'BV', 'LP', 'LLP', 'CO', 'CORP', 'CORPORATION', 'COMPANY', 'THE', 'AND'}
_FIELD = re.compile(r"""<div class=["']title["']><span>(.*?)</span></div>"""
                    r"""<div class=["']value[^"']*["']><span>(.*?)</span>""", re.S)
_TOTAL = re.compile(r'of\s+(\d+)\s+records')


class RegisterError(RuntimeError):
    pass


@dataclass(frozen=True)
class Registered:
    """One registered institution, as institutions.Institution is for the hand list."""
    name: str
    type: str
    group: str
    fragments: tuple[str, ...] = ()
    source: str = 'register'


def norm(name) -> str:
    """A name for matching: capitals and digits, without punctuation or legal suffixes,
    'MF' spelled out. '' when too short to match safely."""
    words = re.sub(r'[^A-Z0-9 ]', ' ', str(name or '').upper().replace('&', ' AND ')).split()
    while words and words[0] == 'THE':
        words.pop(0)
    while words and words[-1] in SUFFIXES:
        words.pop()
    if words and words[-1] == 'MF':
        words[-1:] = ['MUTUAL', 'FUND']
    out = ''.join(words)
    return out if len(out) >= 6 else ''


def parse_page(body: str) -> tuple[list[dict], int | None]:
    """(records, total) of one of SEBI's register pages: each record's name,
    registration number, address, validity and country."""
    total = _TOTAL.search(body)
    out = []
    for block in body.split('fixed-table-body card-table')[1:]:
        fields = {html.unescape(re.sub(r'<[^>]+>', '', k)).strip():
                  html.unescape(re.sub(r'<[^>]+>', '', v)).strip()
                  for k, v in _FIELD.findall(block)}
        if fields.get('Name') and fields.get('Registration No.'):
            out.append({'name': re.sub(r'\s+', ' ', fields['Name']),
                        'reg_no': fields['Registration No.'],
                        'country': fields.get('Country Name') or None,
                        'validity': fields.get('Validity') or None})
    return out, int(total.group(1)) if total else None


def fetch(source: str, *, client: httpx.Client | None = None, pause=time.sleep) -> list[dict]:
    """Every record of one SEBI register, page by page, as its own page asks for them (with
    the page's session cookie). RegisterError when SEBI refuses or the list is short."""
    spec = SOURCES[source]
    own = client is None
    client = client or httpx.Client(headers={'User-Agent': USER_AGENT}, timeout=40,
                                    follow_redirects=True)
    page = PAGE.format(id=spec['intm_id'])
    try:
        first = client.get(page)
        if first.status_code != 200:
            raise RegisterError(f"SEBI's {spec['what']} list returned HTTP {first.status_code}")
        records, total = parse_page(first.text)
        if not total or not records:
            raise RegisterError(f"SEBI's {spec['what']} list has changed its layout")
        pages = -(-total // len(records))
        for n in range(1, pages):
            pause(PAUSE_S)
            got = client.post(AJAX, headers={'Referer': page, 'Origin': BASE}, data={
                'nextValue': '1', 'next': 'n', 'intmId': str(spec['intm_id']), 'contPer': '',
                'name': '', 'regNo': '', 'email': '', 'location': '', 'exchange': '',
                'affiliate': '', 'alp': '', 'language': '2', 'model': '', 'esgCategory': '',
                'doDirect': str(n), 'intmIds': ''})
            if got.status_code != 200 or 'Unauthorized' in got.text[:3000]:
                raise RegisterError(f"SEBI refused page {n + 1} of its {spec['what']} list "
                                    f"(HTTP {got.status_code})")
            rows, _ = parse_page(got.text)
            if not rows:
                break
            records += rows
    except httpx.HTTPError as exc:
        raise RegisterError(f"SEBI's {spec['what']} list could not be read: "
                            f"{type(exc).__name__}") from None
    finally:
        if own:
            client.close()
    unique = {r['reg_no']: r for r in records}
    if len(unique) < COMPLETE * total:
        raise RegisterError(f"SEBI's {spec['what']} list came back incomplete: "
                            f"{len(unique)} of {total}")
    return list(unique.values())


def store(conn, source: str, records: list[dict], now: dt.datetime) -> dict:
    """Keep one register's records: new ones added, ones no longer listed marked inactive.
    Returns the names added and removed."""
    spec = SOURCES[source]
    before = dict(conn.execute('select reg_no, active from institution_register '
                               'where source = %s', (source,)).fetchall())
    added = []
    for r in records:
        if before.get(r['reg_no']) is not True:
            added.append(r['name'])
        conn.execute("""insert into institution_register (source, reg_no, name, norm, type,
                grp, country, validity, first_seen_at, last_seen_at, active)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
            on conflict (source, reg_no) do update set name = excluded.name,
                norm = excluded.norm, country = excluded.country,
                validity = excluded.validity, last_seen_at = excluded.last_seen_at,
                active = true""",
                     (source, r['reg_no'], r['name'], norm(r['name']), spec['type'],
                      spec['group'], r['country'], r['validity'], now, now))
    listed = {r['reg_no'] for r in records}
    gone = [k for k, active in before.items() if active and k not in listed]
    removed = [n for (n,) in conn.execute(
        'select name from institution_register where source = %s and reg_no = any(%s)',
        (source, gone)).fetchall()] if gone else []
    if gone:
        conn.execute('update institution_register set active = false '
                     'where source = %s and reg_no = any(%s)', (source, gone))
    return {'total': len(records), 'added': added, 'removed': removed,
            'first': not before}


def summary(results: dict) -> str | None:
    """The Telegram message for a refresh, or None when nothing changed."""
    lines = []
    for source, r in results.items():
        what = SOURCES[source]['what']
        if 'error' in r:
            lines.append(f"{what.capitalize()}: not refreshed ({r['error']}); the last list "
                         "stays in use.")
            continue
        if r['first']:
            lines.append(f"{what.capitalize()}: {r['total']:,} loaded.")
            continue
        if not r['added'] and not r['removed']:
            continue
        lines.append(f"{what.capitalize()}: {r['total']:,} registered; "
                     f"{len(r['added'])} new, {len(r['removed'])} no longer listed.")
        for label, names in (('New', r['added']), ('No longer listed', r['removed'])):
            if names:
                shown = names[:15]
                more = f' and {len(names) - 15} more' if len(names) > 15 else ''
                lines.append(f"{label}: {'; '.join(shown)}{more}.")
    if not lines:
        return None
    return '\n'.join(["FII/DII register updated from SEBI's lists", *lines,
                      'Their bulk and block deals and takeover-code filings are sent here.'])


def refresh(conn, *, sources=tuple(SOURCES), client=None, pause=time.sleep, notify=None,
            now: dt.datetime | None = None) -> dict:
    """Read SEBI's registers and keep them; send what changed. A register SEBI refuses or
    serves short is left as it was."""
    now = now or utc_now()
    results = {}
    for source in sources:
        run = conn.execute("""insert into institution_register_run (source, started_at,
            status) values (%s, %s, 'running') returning run_id""", (source, now)).fetchone()[0]
        conn.commit()
        try:
            records = fetch(source, client=client, pause=pause)
            with conn.transaction():
                r = store(conn, source, records, now)
            results[source] = r
            conn.execute("""update institution_register_run set finished_at = %s,
                status = 'ok', total = %s, added = %s, removed = %s where run_id = %s""",
                         (utc_now(), r['total'], len(r['added']), len(r['removed']), run))
        except RegisterError as exc:
            conn.rollback()
            results[source] = {'error': str(exc)}
            conn.execute("""update institution_register_run set finished_at = %s,
                status = 'failed', note = %s where run_id = %s""", (utc_now(), str(exc), run))
            from igs.alerts.operations import record_issue
            record_issue('institution-register', 'RegisterError')
        conn.commit()
    text = summary(results)
    if text:
        if notify is None:
            from igs.alerts.delivery import send_deals as notify
        notify(text)
    return results


# --------------------------------------------------------------------------- matching


@lru_cache(maxsize=1)
def _excluded(path: Path | None = None) -> tuple[str, ...]:
    import yaml

    from igs.config import config_dir
    data = yaml.safe_load((path or config_dir() / 'institutions.yaml').read_text('utf-8'))
    return tuple(re.sub(r'[^A-Z0-9]', '', str(e).upper()) for e in data.get('exclude') or ())


def lookup(conn):
    """A function from a deal file's client name to the registered institution it is, or
    None: FPIs by exact name, mutual funds by name within the client's."""
    rows = conn.execute("""select source, name, norm, type, grp from institution_register
        where active and norm <> ''""").fetchall()
    fpis = {}
    funds = []
    for source, name, key, type_, grp in rows:
        inst = Registered(name, type_, grp, (key,))
        if source == 'sebi_mf':
            funds.append((key, inst))
        else:
            fpis.setdefault(key, inst)
    funds.sort(key=lambda kv: -len(kv[0]))
    excluded = _excluded()

    def find(client: str):
        flat = re.sub(r'[^A-Z0-9]', '', str(client).upper())
        if any(x and x in flat for x in excluded):
            return None
        hit = fpis.get(norm(client))
        if hit:
            return hit
        return next((inst for key, inst in funds if key in flat), None)
    return find


def status(conn) -> list[dict]:
    """Each register's size and latest refresh, for the page."""
    rows = conn.execute("""select s.source,
            (select count(*) from institution_register r where r.source = s.source
             and r.active),
            (select max(finished_at) from institution_register_run x
             where x.source = s.source and x.status = 'ok'),
            (select status from institution_register_run x where x.source = s.source
             order by started_at desc, run_id desc limit 1)
        from (values ('sebi_fpi'), ('sebi_mf')) s(source)
        order by s.source desc""").fetchall()
    return [{'list': SOURCES[s]['what'], 'registered': n,
             'last refreshed (IST)': t.astimezone(IST).replace(tzinfo=None) if t else None,
             'last run': last} for s, n, t, last in rows]
