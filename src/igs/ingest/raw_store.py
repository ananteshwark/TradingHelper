"""Immutable raw landing zone.

Every payload the system ever fetches (or a user imports) is stored verbatim,
exactly once, before anything parses it:

    <root>/blobs/<sha[:2]>/<sha256>                         payload bytes
    <root>/fetches/<source>/<yyyy>/<mm>/<dd>/<fetch_id>.json   fetch record

Blobs are content-addressed and shared by identical re-fetches. Fetch records
say who fetched what, from where, when, and with what HTTP status. Both are
created with exclusive-create and then made read-only; nothing here is ever
rewritten. The raw_payload table is an index of the fetch records and can be
rebuilt from disk with `reindex_into_db`, so every table downstream is
rebuildable from this directory alone.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from igs.timeutil import UTC, require_aware, utc_now

_READ_ONLY = 0o444


class RawStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchRecord:
    fetch_id: str
    source_id: str
    url: str | None
    fetched_at: dt.datetime
    http_status: int | None
    content_sha256: str
    size_bytes: int
    content_type: str | None
    blob_path: str
    origin: Literal["http", "manual"] = "http"
    request_params: dict[str, Any] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        d["fetched_at"] = self.fetched_at.astimezone(UTC).isoformat()
        return json.dumps(d, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> FetchRecord:
        d = json.loads(text)
        d["fetched_at"] = dt.datetime.fromisoformat(d["fetched_at"])
        return cls(**d)


def _fetch_id(source_id: str, fetched_at: dt.datetime, sha: str) -> str:
    if "__" in source_id or "/" in source_id:
        raise RawStoreError(f"source_id may not contain '__' or '/': {source_id!r}")
    return f"{source_id}__{fetched_at.astimezone(UTC):%Y%m%dT%H%M%S%fZ}__{sha[:12]}"


def _write_once(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as fh:  # exclusive create: fail rather than overwrite
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, _READ_ONLY)


class RawStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ write

    def put(
        self,
        *,
        source_id: str,
        content: bytes,
        url: str | None,
        http_status: int | None,
        content_type: str | None = None,
        fetched_at: dt.datetime | None = None,
        origin: Literal["http", "manual"] = "http",
        request_params: dict[str, Any] | None = None,
        response_headers: dict[str, str] | None = None,
        note: str = "",
    ) -> FetchRecord:
        fetched_at = require_aware(fetched_at or utc_now(), "fetched_at")
        sha = hashlib.sha256(content).hexdigest()
        blob_rel = Path("blobs") / sha[:2] / sha
        blob_abs = self.root / blob_rel
        if blob_abs.exists():
            if self._sha_of(blob_abs) != sha:
                raise RawStoreError(f"blob {blob_abs} is corrupt: hash mismatch")
        else:
            _write_once(blob_abs, content)

        record = FetchRecord(
            fetch_id=_fetch_id(source_id, fetched_at, sha),
            source_id=source_id,
            url=url,
            fetched_at=fetched_at.astimezone(UTC),
            http_status=http_status,
            content_sha256=sha,
            size_bytes=len(content),
            content_type=content_type,
            blob_path=str(blob_rel),
            origin=origin,
            request_params=dict(request_params or {}),
            response_headers=dict(response_headers or {}),
            note=note,
        )
        rec_path = self._record_path(record.fetch_id)
        try:
            _write_once(rec_path, record.to_json().encode())
        except FileExistsError as exc:
            raise RawStoreError(f"fetch record {record.fetch_id} already exists") from exc
        return record

    def put_file(self, *, source_id: str, path: Path | str, note: str = "") -> FetchRecord:
        """Land a user-supplied file (e.g. a Screener.in export) verbatim."""
        path = Path(path)
        return self.put(
            source_id=source_id,
            content=path.read_bytes(),
            url=None,
            http_status=None,
            origin="manual",
            request_params={"original_filename": path.name},
            note=note,
        )

    # ------------------------------------------------------------------ read

    def get(self, fetch_id: str) -> FetchRecord:
        path = self._record_path(fetch_id)
        if not path.exists():
            raise KeyError(fetch_id)
        return FetchRecord.from_json(path.read_text(encoding="utf-8"))

    def read_bytes(self, record: FetchRecord | str) -> bytes:
        if isinstance(record, str):
            record = self.get(record)
        data = (self.root / record.blob_path).read_bytes()
        if hashlib.sha256(data).hexdigest() != record.content_sha256:
            raise RawStoreError(f"blob for {record.fetch_id} failed hash check")
        return data

    def iter_records(self, source_id: str | None = None) -> Iterator[FetchRecord]:
        base = self.root / "fetches"
        if source_id is not None:
            base = base / source_id
        if not base.exists():
            return
        paths = sorted(base.rglob("*.json"))
        records = [FetchRecord.from_json(p.read_text(encoding="utf-8")) for p in paths]
        yield from sorted(records, key=lambda r: (r.fetched_at, r.fetch_id))

    # ------------------------------------------------------------------ index

    @staticmethod
    def index_record(conn, r: FetchRecord) -> int:
        """Insert one fetch record into raw_payload (idempotent). Returns rows inserted."""
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into raw_payload
                    (fetch_id, source_id, url, fetched_at, http_status, content_sha256,
                     size_bytes, content_type, blob_path, origin, request_params,
                     response_headers, note)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (fetch_id) do nothing
                """,
                (r.fetch_id, r.source_id, r.url, r.fetched_at, r.http_status,
                 r.content_sha256, r.size_bytes, r.content_type, r.blob_path, r.origin,
                 json.dumps(r.request_params), json.dumps(r.response_headers), r.note),
            )
            return cur.rowcount

    def reindex_into_db(self, conn) -> int:
        """Insert every fetch record into raw_payload (idempotent). Returns rows inserted."""
        return sum(self.index_record(conn, r) for r in self.iter_records())

    # ------------------------------------------------------------------ internals

    def _record_path(self, fetch_id: str) -> Path:
        try:
            source_id, stamp, _ = fetch_id.split("__")
        except ValueError as exc:
            raise KeyError(f"malformed fetch_id {fetch_id!r}") from exc
        return (self.root / "fetches" / source_id / stamp[0:4] / stamp[4:6] / stamp[6:8]
                / f"{fetch_id}.json")

    @staticmethod
    def _sha_of(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
