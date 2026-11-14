"""Download again the NSE files the cleanup removed, so the database can be rebuilt.

With its switch on (`delete_loaded_nse_files`, docs/MAINTENANCE.md), the cleanup removes
the bytes of NSE files already loaded into the database: the price files (bhavcopies,
delivery, index closes, market caps) and filing documents NSE keeps at a fixed address.
Nothing reads them again except `igs rebuild`. This brings them back first:

    igs raw restore                 every removed file the rebuild replays
    igs rebuild --download          the same, then the rebuild

Each file is downloaded again from the address in its fetch record and kept only if its
bytes are exactly those first downloaded (the same sha256), so a rebuild gives the same
database. A file NSE now serves differently, or not at all, is reported and left out, and
the rebuild then doesn't start. Each download made again is landed like any other, with
the note "downloaded again for <fetch_id>", and is never replayed itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from igs.ingest.http import Fetcher, FetchError
from igs.ingest.raw_store import RESTORE_NOTE, FetchRecord, RawStore

log = logging.getLogger(__name__)


@dataclass
class Restored:
    restored: int = 0
    changed: list[str] = field(default_factory=list)     # NSE serves other bytes now
    failed: list[str] = field(default_factory=list)      # "fetch_id: why"

    @property
    def complete(self) -> bool:
        return not self.changed and not self.failed


def replay_sources() -> list[str]:
    from igs.ingest.jobs import HANDLERS, POST_MASTER_HANDLERS
    return [*HANDLERS, *POST_MASTER_HANDLERS]


def missing(store: RawStore, sources: list[str] | None = None) -> list[FetchRecord]:
    """The downloads a rebuild replays whose bytes are gone, oldest first."""
    from igs.ingest.jobs import replayable
    out = []
    for source_id in sources or replay_sources():
        for rec in store.iter_records(source_id):
            if replayable(rec) and not (store.root / rec.blob_path).is_file():
                out.append(rec)
    return sorted(out, key=lambda r: (r.fetched_at, r.fetch_id))


def _nse_address(url: str) -> bool:
    """The cleanup deletes only NSE's files, so only NSE's addresses are asked again."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and (host == "nseindia.com" or host.endswith(".nseindia.com"))


def _session(sources, source_id: str) -> str:
    try:
        return sources.get(source_id).session if sources is not None else "none"
    except KeyError:              # filing documents have no entry of their own: no session
        return "none"


def restore(store: RawStore, fetcher: Fetcher, sources=None, *,
            records: list[FetchRecord] | None = None, limit: int | None = None,
            progress: Callable[[str], None] | None = None) -> Restored:
    """Download again each of `records` (default: every missing file the rebuild replays)
    and keep the bytes only if they are exactly the first download's."""
    todo = missing(store) if records is None else records
    if limit is not None:
        todo = todo[:limit]
    out = Restored()
    for i, rec in enumerate(todo, 1):
        if (store.root / rec.blob_path).is_file():     # the same bytes came back already
            out.restored += 1
            continue
        if rec.origin != "http" or not rec.url:
            out.failed.append(f"{rec.fetch_id}: no address to download it from")
            continue
        if not _nse_address(rec.url):
            out.failed.append(f"{rec.fetch_id}: not an NSE address, so not asked again")
            continue
        try:
            got = fetcher.get(rec.source_id, rec.url, _session(sources, rec.source_id),
                              note=f"{RESTORE_NOTE} for {rec.fetch_id}",
                              params=rec.request_params)
        except FetchError as exc:
            out.failed.append(f"{rec.fetch_id}: {exc}")
            continue
        if got.http_status != 200:
            out.failed.append(f"{rec.fetch_id}: HTTP {got.http_status}")
        elif got.content_sha256 != rec.content_sha256:
            out.changed.append(rec.fetch_id)
            log.warning("%s: NSE now serves different bytes at %s; kept as %s, not used",
                        rec.fetch_id, rec.url, got.fetch_id)
        else:
            out.restored += 1
        if progress and (i % 50 == 0 or i == len(todo)):
            progress(f"{i}/{len(todo)} downloaded again")
    return out


def describe(r: Restored) -> str:
    text = f"Downloaded again: {r.restored} file(s) with their first bytes."
    if r.changed:
        text += (f" {len(r.changed)} now differ at NSE and were not used (first "
                 f"{r.changed[0]}).")
    if r.failed:
        text += f" {len(r.failed)} could not be downloaded (first {r.failed[0]})."
    return text
