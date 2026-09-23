"""HTTP fetching. Every response body is landed in the raw store before anyone parses it.

Read-only by construction: only GET is implemented. There is no code path
that places, modifies or cancels orders with any broker.
"""

from __future__ import annotations

import time
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
_KEEP_HEADERS = ("content-type", "content-length", "last-modified", "etag", "date")


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, store: RawStore, *, min_interval_s: float = 1.0,
                 timeout_s: float = 30.0, client: httpx.Client | None = None) -> None:
        self.store = store
        self.min_interval_s = min_interval_s
        self.client = client or httpx.Client(headers=_BROWSER_HEADERS, timeout=timeout_s,
                                             follow_redirects=True)
        self._last_request = 0.0
        self._nse_primed = False

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
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
        """GET url and land the response verbatim, whatever its status."""
        try:
            extra = self._prime(session)
            self._throttle()
            resp = self.client.get(url, headers=extra)
        except httpx.HTTPError as exc:
            raise FetchError(f"{source_id}: {url}: {exc}") from exc
        headers = {k: v for k, v in resp.headers.items() if k.lower() in _KEEP_HEADERS}
        return self.store.put(
            source_id=source_id,
            content=resp.content,
            url=url,
            http_status=resp.status_code,
            content_type=resp.headers.get("content-type"),
            response_headers=headers,
            request_params=params,
            note=note,
        )
