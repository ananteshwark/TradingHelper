"""HTTP fetching. Every response body is landed in the raw store before anyone parses it.

Read-only by construction: only GET is implemented. There is no code path
that places, modifies or cancels orders with any broker.

Transient failures (connection errors, timeouts, HTTP 429 and 5xx) are retried
with exponential backoff and jitter; every attempt's response is landed, so a
flaky endpoint leaves evidence rather than a silent gap. Anything else (404, a
changed format) is not retried: it is a real answer and is handled by the caller
and the verification gate.

Politeness, from what the live exchanges did (see HOST_MIN_INTERVAL_S):
  * requests to NSE hosts are spaced at least 5 seconds apart; bursts at one
    second trip Akamai's rate limit, which then refuses every request from the
    address for about five minutes;
  * Akamai's "Access Denied" 403 is treated as that throttle: the fetcher waits
    `throttle_wait_s` and tries again (at most `max_throttle_waits` times), once per host
    per run: a host whose wait did not help is not waited for again;
  * "Access Denied" can also be one endpoint's own refusal: on 2026-09-23 the quote API
    was denied while, seconds later, the results, shareholding and announcement APIs on
    the same host answered. So an endpoint (host and path) still denied after the wait is
    not asked again in this run, and a whole host is given up only after
    HOST_BLOCK_AFTER denials in a row on it with no answer in between. Requests to either
    raise FetchError at once, so a blocked run stops instead of knocking on;
  * NSE cookie priming is attempted once; if the home page itself is refused,
    it is not repeated (the data endpoints were observed to answer without it).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Literal

import httpx

from igs.ingest.raw_store import FetchRecord, RawStore

SessionKind = Literal["none", "nse_cookie", "bse_referer"]

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
_KEEP_HEADERS = ("content-type", "content-length", "last-modified", "etag", "date",
                 "retry-after")
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Minimum seconds between requests to a host (suffix match); others use min_interval_s.
HOST_MIN_INTERVAL_S = {"nseindia.com": 5.0, "bseindia.com": 3.0}
AKAMAI_DENIED = b"Access Denied"
HOST_BLOCK_AFTER = 3        # denials in a row on a host, after its wait, before giving it up
log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, store: RawStore, *, min_interval_s: float = 1.0,
                 timeout_s: float = 30.0, client: httpx.Client | None = None,
                 max_attempts: int = 4, backoff_s: float = 2.0, max_backoff_s: float = 60.0,
                 throttle_wait_s: float = 330.0, max_throttle_waits: int = 1,
                 host_min_interval_s: dict[str, float] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self.min_interval_s = min_interval_s
        self.client = client or httpx.Client(headers=_BROWSER_HEADERS, timeout=timeout_s,
                                             follow_redirects=True)
        self.max_attempts = max(1, max_attempts)
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.throttle_wait_s = throttle_wait_s
        self.max_throttle_waits = max(0, max_throttle_waits)
        self.host_min_interval_s = (HOST_MIN_INTERVAL_S if host_min_interval_s is None
                                    else host_min_interval_s)
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._nse_primed = False
        self._nse_prime_ok = False
        self.blocked_hosts: set[str] = set()
        self.blocked_paths: set[str] = set()     # "host/path" still denied after the wait
        self._waited: set[str] = set()           # hosts whose throttle wait did not help
        self._denied_in_row: dict[str, int] = {}

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after), self.max_backoff_s)
        base = min(self.backoff_s * 2 ** (attempt - 1), self.max_backoff_s)
        return base + random.uniform(0, base / 2)

    def _interval(self, host: str) -> float:
        for suffix, s in self.host_min_interval_s.items():
            if host == suffix or host.endswith("." + suffix):
                return max(s, self.min_interval_s)
        return self.min_interval_s

    def _throttle(self, url: str = "") -> None:
        host = httpx.URL(url).host if url else ""
        key = next((sfx for sfx in self.host_min_interval_s
                    if host == sfx or host.endswith("." + sfx)), host)
        wait = self._interval(host) - (time.monotonic() - self._last_request.get(key, -1e9))
        if wait > 0:
            self._sleep(wait)
        self._last_request[key] = time.monotonic()

    def _prime(self, session: SessionKind) -> dict[str, str]:
        if session == "nse_cookie":
            # Cookie priming only: the home page is not data and is not landed. Tried once;
            # a refused home page is not retried (it would only spend the rate budget).
            if not self._nse_primed:
                home = "https://www.nseindia.com/"
                self._throttle(home)
                self._nse_prime_ok = self.client.get(home).status_code == 200
                self._nse_primed = True
            return {"Referer": "https://www.nseindia.com/"}
        if session == "bse_referer":
            return {"Referer": "https://www.bseindia.com/"}
        return {}

    def get(self, source_id: str, url: str, session: SessionKind = "none",
            note: str = "", params: dict | None = None) -> FetchRecord:
        """GET url and land the response verbatim, whatever its status. Transient failures
        are retried (each attempt landed); the last attempt's record is returned."""
        parsed = httpx.URL(url)
        host, endpoint = parsed.host, parsed.host + parsed.path
        if host in self.blocked_hosts:
            raise FetchError(f"{source_id}: {host} refused {HOST_BLOCK_AFTER} requests in a row "
                             "(Access Denied) after the throttle wait earlier in this run; "
                             "not asked again")
        if endpoint in self.blocked_paths:
            raise FetchError(f"{source_id}: {endpoint} was still refused (Access Denied) after "
                             "the throttle wait earlier in this run; not asked again")
        reprimed = False
        throttle_waits = 0
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            last = attempt == self.max_attempts
            try:
                extra = self._prime(session)
                self._throttle(url)
                resp = self.client.get(url, headers=extra)
            except httpx.TransportError as exc:
                if last:
                    raise FetchError(f"{source_id}: {url}: {exc} (after {attempt} attempts)"
                                     ) from exc
                self._sleep(self._backoff(attempt))
                continue
            except httpx.HTTPError as exc:
                raise FetchError(f"{source_id}: {url}: {exc}") from exc
            status = resp.status_code
            denied = status == 403 and AKAMAI_DENIED in resp.content[:4000]
            throttled = (denied and throttle_waits < self.max_throttle_waits
                         and host not in self._waited)
            retry_nse = (session == "nse_cookie" and status in (401, 403) and not reprimed
                         and self._nse_prime_ok and not throttled)
            retry = throttled or ((status in RETRY_STATUS or retry_nse) and not last)
            headers = {k: v for k, v in resp.headers.items() if k.lower() in _KEEP_HEADERS}
            rec = self.store.put(
                source_id=source_id,
                content=resp.content,
                url=url,
                http_status=status,
                content_type=resp.headers.get("content-type"),
                response_headers=headers,
                request_params=params,
                note=_retry_note(note, attempt) if retry else note,
            )
            if not retry:
                self._note_denial(host, endpoint, denied, waited=throttle_waits > 0)
                return rec
            if throttled:
                # Akamai's rate limit: wait it out rather than hammer the address.
                log.warning("%s: %s answered 403 Access Denied; waiting %.0f s before one "
                            "more try (NSE's rate limit; this is not a hang)",
                            source_id, endpoint, self.throttle_wait_s)
                throttle_waits += 1
                attempt -= 1                     # a throttle wait is not a failed attempt
                self._sleep(self.throttle_wait_s)
                continue
            if retry_nse:
                reprimed, self._nse_primed = True, False
                continue
            self._sleep(self._backoff(attempt, resp.headers.get("retry-after")))
        raise AssertionError("unreachable")

    def _note_denial(self, host: str, endpoint: str, denied: bool, waited: bool) -> None:
        if not denied:
            self._denied_in_row[host] = 0
            return
        self._denied_in_row[host] = self._denied_in_row.get(host, 0) + 1
        if waited:
            self._waited.add(host)
        if host in self._waited:        # waiting did not help: do not ask this endpoint again
            self.blocked_paths.add(endpoint)
            if self._denied_in_row[host] >= HOST_BLOCK_AFTER:
                self.blocked_hosts.add(host)
            log.warning("%s still refused after the wait; not asked again in this run%s",
                        endpoint, " (nor anything else on " + host + ")"
                        if host in self.blocked_hosts else "")


def _retry_note(note: str, attempt: int) -> str:
    tag = f"attempt {attempt}, retried"
    return f"{note}; {tag}" if note else tag
