"""HTTP fetching. Every response body is landed in the raw store before anyone parses it.

Read-only by construction: only GET is implemented. There is no code path
that places, modifies or cancels orders with any broker.

Transient failures (connection errors, timeouts, HTTP 429 and 5xx) are retried
with exponential backoff and jitter; every attempt's response is landed, so a
flaky endpoint leaves evidence rather than a silent gap. An NSE 401/403 re-primes
the session cookies once. Anything else (404, a changed format) is not retried:
it is a real answer and is handled by the caller and the verification gate.
"""

from __future__ import annotations

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


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, store: RawStore, *, min_interval_s: float = 1.0,
                 timeout_s: float = 30.0, client: httpx.Client | None = None,
                 max_attempts: int = 4, backoff_s: float = 2.0, max_backoff_s: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self.min_interval_s = min_interval_s
        self.client = client or httpx.Client(headers=_BROWSER_HEADERS, timeout=timeout_s,
                                             follow_redirects=True)
        self.max_attempts = max(1, max_attempts)
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self._sleep = sleep
        self._last_request = 0.0
        self._nse_primed = False

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after), self.max_backoff_s)
        base = min(self.backoff_s * 2 ** (attempt - 1), self.max_backoff_s)
        return base + random.uniform(0, base / 2)

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def _prime(self, session: SessionKind) -> dict[str, str]:
        if session == "nse_cookie":
            # Cookie priming only: the home page is not data and is not landed.
            if not self._nse_primed:
                self._throttle()
                self.client.get("https://www.nseindia.com/")
                self._nse_primed = True
            return {"Referer": "https://www.nseindia.com/"}
        if session == "bse_referer":
            return {"Referer": "https://www.bseindia.com/"}
        return {}

    def get(self, source_id: str, url: str, session: SessionKind = "none",
            note: str = "", params: dict | None = None) -> FetchRecord:
        """GET url and land the response verbatim, whatever its status. Transient failures
        are retried (each attempt landed); the last attempt's record is returned."""
        reprimed = False
        for attempt in range(1, self.max_attempts + 1):
            last = attempt == self.max_attempts
            try:
                extra = self._prime(session)
                self._throttle()
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
            retry_nse = session == "nse_cookie" and status in (401, 403) and not reprimed
            retry = (status in RETRY_STATUS or retry_nse) and not last
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
                return rec
            if retry_nse:
                reprimed, self._nse_primed = True, False
                continue
            self._sleep(self._backoff(attempt, resp.headers.get("retry-after")))
        raise AssertionError("unreachable")


def _retry_note(note: str, attempt: int) -> str:
    tag = f"attempt {attempt}, retried"
    return f"{note}; {tag}" if note else tag
