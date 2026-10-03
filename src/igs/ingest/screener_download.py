"""Authenticated, rate-limited use of Screener's offered Excel export form."""
from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

ORIGIN = 'https://www.screener.in'
MAX_BYTES = 20_000_000


class DownloadError(RuntimeError):
    """Safe messages only: never response bodies or credentials."""


class LoginRequired(DownloadError):
    pass


class AccessLimited(DownloadError):
    pass


class Form(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.csrf = None
        self.export = None
        self.password = False
        self.symbols = set()
        self.bse_codes = set()
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        a = dict(attributes)
        if tag == 'input':
            if a.get('name') == 'csrfmiddlewaretoken':
                self.csrf = a.get('value')
            if a.get('name') == 'password':
                self.password = True
        if tag == 'button' and re.fullmatch(r'/user/company/export/\d+/',
                                            a.get('formaction', '')):
            self.export = a['formaction']
        if tag == 'a':
            url = urlsplit(a.get('href', ''))
            if url.hostname in ('www.nseindia.com', 'nseindia.com'):
                self.symbols.update(parse_qs(url.query).get('symbol', []))
            if url.hostname in ('www.bseindia.com', 'bseindia.com'):
                code = url.path.rstrip('/').split('/')[-1]
                if code.isdigit():
                    self.bse_codes.add(code)
                self.bse_codes.update(parse_qs(url.query).get('scripcode', []))


class Client:
    def __init__(self, client=None, *, delay=10, sleep=time.sleep):
        self.http = client or httpx.Client(timeout=45, follow_redirects=False,
            headers={'User-Agent': 'TradingHelper personal Excel export downloader'})
        self.delay, self.sleep = delay, sleep
        self.last_request = None

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        url = urljoin(ORIGIN, path)
        for _ in range(6):
            parts = urlsplit(url)
            if parts.scheme != 'https' or parts.netloc != 'www.screener.in':
                raise AccessLimited('Screener redirected outside its origin; download paused')
            if self.last_request is not None:
                self.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            try:
                with self.http.stream(method, url, **kwargs) as response:
                    self.last_request = time.monotonic()
                    if response.status_code in (401,):
                        raise LoginRequired('Screener sign-in is required')
                    if response.status_code in (403, 429):
                        raise AccessLimited('Screener access or download limit reached')
                    if response.is_redirect:
                        # Never forward a login POST body through a redirect.
                        url = urljoin(url, response.headers.get('location', ''))
                        method, kwargs = 'GET', {}
                        continue
                    if response.status_code == 404:
                        raise FileNotFoundError('Company page not found on Screener')
                    if response.status_code != 200:
                        raise DownloadError(f'Screener returned HTTP {response.status_code}')
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise DownloadError('Screener response exceeds size limit')
                        chunks.append(chunk)
                    return b''.join(chunks), url
            except httpx.HTTPError:
                raise DownloadError('Screener network request failed') from None
        raise DownloadError('Screener redirect limit exceeded')

    def login(self, email, password):
        content, _ = self.request('GET', '/login/')
        form = Form(content.decode('utf-8', errors='replace'))
        if not form.csrf:
            raise AccessLimited('Screener login form unavailable; manual check required')
        content, url = self.request('POST', '/login/', data={
            'username': email, 'password': password, 'csrfmiddlewaretoken': form.csrf},
            headers={'Referer': ORIGIN + '/login/'})
        if (Form(content.decode('utf-8', errors='replace')).password
                or '/login/' in url
                or not any(c.name == 'sessionid' for c in self.http.cookies.jar)):
            raise LoginRequired('Screener rejected sign-in; run igs screener configure')

    def download(self, symbol, id_type='NSE_SYMBOL'):
        path = '/company/' + quote(symbol, safe='') + '/consolidated/'
        for candidate in (path, path.removesuffix('consolidated/')):
            try:
                content, page = self.request('GET', candidate)
            except FileNotFoundError:
                if candidate == path:
                    continue
                raise
            form = Form(content.decode('utf-8', errors='replace'))
            if form.password:
                raise LoginRequired('Screener session expired')
            codes = form.symbols if id_type=='NSE_SYMBOL' else form.bse_codes
            if symbol not in codes:
                raise DownloadError('Company page does not confirm the requested exchange code')
            if form.csrf and form.export:
                break
            # Some companies return an empty consolidated page with HTTP 200.
            # Only use a normal standalone export; access limits still propagate.
        if not form.csrf or not form.export:
            raise AccessLimited('Screener export form unavailable; manual check required')
        content, url = self.request('POST', form.export,
            data={'csrfmiddlewaretoken': form.csrf}, headers={'Referer': page})
        if not content.startswith(b'PK'):
            if Form(content.decode('utf-8', errors='replace')).password:
                raise LoginRequired('Screener export requires sign-in')
            raise AccessLimited('Screener did not return an Excel export; check account limits')
        return content, url
