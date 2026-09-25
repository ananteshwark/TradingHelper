"""Source verification: prove an endpoint returns real data before any code depends on it.

`verify_source` fetches a sample, lands it in the raw store, checks that the
payload parses in the declared format, and records a schema fingerprint
(CSV header or JSON keys). Verification records are write-once JSON files
under <raw_root>/verifications/, so they are rebuildable state like
everything else.

`require_verified` is the ingestion gate: it refuses a source whose latest
verification is missing or failed, and `check_fingerprint` refuses a payload
whose schema differs from the verified one, so a silent format change stops
ingestion loudly instead of producing wrong numbers.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from igs.config import SourceSpec
from igs.ingest.http import Fetcher, FetchError
from igs.ingest.raw_store import _write_once
from igs.ingest.sources import SourceNotReady, paging, recent_weekdays, render_url
from igs.timeutil import IST, utc_now


class ProbeError(ValueError):
    pass


class SourceNotVerified(RuntimeError):
    pass


@dataclass(frozen=True)
class Verification:
    source_id: str
    url_template: str | None
    checked_at: str
    status: Literal["verified", "failed"]
    message: str
    url: str | None = None
    http_status: int | None = None
    fetch_id: str | None = None
    fingerprint: str | None = None
    schema: list[str] | None = None
    row_count: int | None = None


# --------------------------------------------------------------------------- probing


def _csv_header(text: str) -> tuple[list[str], int]:
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        raise ProbeError("CSV has no data rows")
    return [c.strip() for c in rows[0]], len(rows) - 1


def _json_schema(obj: object) -> tuple[list[str], int]:
    if isinstance(obj, list):
        if not obj:
            raise ProbeError("JSON list is empty")
        first = obj[0]
        return (sorted(first.keys()) if isinstance(first, dict) else ["<scalar>"]), len(obj)
    if isinstance(obj, dict):
        if not obj:
            raise ProbeError("JSON object is empty")
        keys = sorted(obj.keys())
        data = obj.get("data")
        if isinstance(data, list) and not data:
            raise ProbeError("JSON data list is empty")
        if isinstance(data, list) and isinstance(data[0], dict):
            return keys + [f"data.{k}" for k in sorted(data[0].keys())], len(data)
        return keys, 1
    raise ProbeError(f"unexpected JSON top-level type {type(obj).__name__}")


def probe(fmt: str, content: bytes) -> tuple[list[str], int]:
    """Return (schema, row_count) or raise ProbeError."""
    if not content:
        raise ProbeError("empty body")
    head = content.lstrip()[:15].lower()
    if fmt == "xml":
        if not head.startswith((b"<?xml", b"<xbrl", b"<xbrli")):
            raise ProbeError("expected an XML document")
        root_tag = content.lstrip()[:400].decode("utf-8", errors="replace")
        return [root_tag.split("?>", 1)[-1].strip()[:80]], 1
    if head.startswith((b"<!doctype", b"<html", b"<?xml")):
        raise ProbeError("got an HTML/XML page where data was expected (blocked or moved?)")
    if fmt == "zip_csv":
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise ProbeError("not a zip archive") from exc
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise ProbeError(f"zip has no CSV member: {zf.namelist()}")
        return _csv_header(zf.read(members[0]).decode("utf-8-sig", errors="replace"))
    if fmt == "csv":
        return _csv_header(content.decode("utf-8-sig", errors="replace"))
    if fmt == "json":
        try:
            return _json_schema(json.loads(content))
        except json.JSONDecodeError as exc:
            raise ProbeError(f"invalid JSON: {exc}") from exc
    if fmt == "text":
        lines = [ln for ln in content.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        if len(lines) < 2:
            raise ProbeError("text file has fewer than 2 lines")
        header = next((ln for ln in lines if _is_text_header(ln)), None)
        if header is None:
            raise ProbeError("no comma-separated header line found")
        return [c.strip() for c in header.split(",")], len(lines)
    raise ProbeError(f"unknown format {fmt}")


def _is_text_header(line: str) -> bool:
    """At least five comma-separated fields, every non-empty one containing a letter."""
    fields = [f.strip() for f in line.split(",")]
    return len(fields) >= 5 and all(any(c.isalpha() for c in f) for f in fields if f)


def fingerprint(schema: list[str]) -> str:
    return hashlib.sha256("|".join(schema).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- records


def _verification_dir(raw_root: Path, source_id: str) -> Path:
    return raw_root / "verifications" / source_id


def save_verification(raw_root: Path, v: Verification) -> Path:
    stamp = v.checked_at.replace(":", "").replace("-", "").replace("+", "_")
    path = _verification_dir(raw_root, v.source_id) / f"{stamp}.json"
    _write_once(path, json.dumps(asdict(v), indent=2).encode())
    return path


def latest_verification(raw_root: Path, source_id: str) -> Verification | None:
    d = _verification_dir(raw_root, source_id)
    if not d.exists():
        return None
    records = [Verification(**json.loads(p.read_text(encoding="utf-8"))) for p in d.glob("*.json")]
    return max(records, key=lambda v: v.checked_at) if records else None


def require_verified(raw_root: Path, spec: SourceSpec) -> Verification:
    v = latest_verification(raw_root, spec.id)
    if v is None:
        raise SourceNotVerified(f"{spec.id}: never verified; run `igs sources verify {spec.id}`")
    if v.status != "verified":
        raise SourceNotVerified(f"{spec.id}: latest verification failed: {v.message}")
    if v.url_template != spec.url:
        raise SourceNotVerified(f"{spec.id}: URL changed since verification; verify again")
    return v


def _no_rows(fmt: str, content: bytes, v: Verification) -> bool:
    """An empty result: an empty JSON list, or the verified envelope around an empty "data"
    list (a paged listing past its last page). Nothing to fingerprint and nothing to load;
    verification itself never accepts one (probe refuses it)."""
    if fmt != "json":
        return False
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return False
    if obj == []:
        return True
    envelope = [k for k in (v.schema or []) if not k.startswith("data.")]
    return isinstance(obj, dict) and obj.get("data") == [] and sorted(obj) == envelope


def check_fingerprint(spec: SourceSpec, content: bytes, v: Verification) -> None:
    if _no_rows(spec.format, content, v):
        return
    try:
        schema, _ = probe(spec.format, content)
    except ProbeError as exc:
        raise SourceNotVerified(f"{spec.id}: schema changed since verification: {exc}\n"
                                f"  verified: {v.schema}") from exc
    if fingerprint(schema) != v.fingerprint:
        raise SourceNotVerified(
            f"{spec.id}: schema changed since verification.\n  verified: {v.schema}\n"
            f"  now:      {schema}")


# --------------------------------------------------------------------------- verification


PROBE_NOTE = "verification probe"


def _attempt(spec: SourceSpec, fetcher: Fetcher, url: str, params: dict) -> Verification:
    now = utc_now().isoformat()
    try:
        rec = fetcher.get(spec.id, url, spec.session, note=PROBE_NOTE, params=params)
    except FetchError as exc:
        return Verification(spec.id, spec.url, now, "failed", f"fetch error: {exc}", url=url)
    base = dict(source_id=spec.id, url_template=spec.url, checked_at=now, url=url,
                http_status=rec.http_status, fetch_id=rec.fetch_id)
    if rec.http_status != 200:
        return Verification(**base, status="failed", message=f"HTTP {rec.http_status}")
    try:
        schema, rows = probe(spec.format, fetcher.store.read_bytes(rec))
    except ProbeError as exc:
        return Verification(**base, status="failed", message=f"probe failed: {exc}")
    return Verification(**base, status="verified", message="ok", fingerprint=fingerprint(schema),
                        schema=schema, row_count=rows)


def verify_source(spec: SourceSpec, fetcher: Fetcher, today: dt.date | None = None,
                  max_dates: int = 7) -> Verification:
    today = today or dt.datetime.now(IST).date()
    try:
        if spec.kind == "static":
            urls = [(render_url(spec), {})]
        elif spec.kind == "paged":
            urls = [(render_url(spec), {"page": paging(spec).first_page})]
        elif spec.kind == "per_symbol":
            sym = spec.probe_symbol or "RELIANCE"
            urls = [(render_url(spec, symbol=sym), {"symbol": sym})]
        elif spec.kind == "date_range":
            start = today - dt.timedelta(days=7)
            urls = [(render_url(spec, start=start, end=today),
                     {"start": start.isoformat(), "end": today.isoformat()})]
        else:
            days = [spec.probe_date] if spec.probe_date else recent_weekdays(today, max_dates)
            urls = [(render_url(spec, day=d), {"date": d.isoformat()}) for d in days]
    except SourceNotReady as exc:
        return Verification(spec.id, spec.url, utc_now().isoformat(), "failed", str(exc))

    result: Verification | None = None
    for url, params in urls:
        result = _attempt(spec, fetcher, url, params)
        if result.status == "verified":
            break
        # A date file can legitimately be missing on a holiday; try the previous weekday.
        if spec.kind != "date_file" or result.http_status not in (404, 403):
            break
    assert result is not None
    save_verification(fetcher.store.root, result)
    return result
