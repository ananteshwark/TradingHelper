"""Process and replay downloaded documents without changing immutable raw payloads."""
from __future__ import annotations

import hashlib
from pathlib import Path

from igs.config import config_dir
from igs.dq import DQLog
from igs.xbrl.load import load_document


def parser_version() -> str:
    root = Path(__file__).resolve().parents[1] / "xbrl"
    h = hashlib.sha256()
    for path in [*sorted(root.glob("*.py")), config_dir() / "xbrl_concepts.yaml"]:
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def process_document(ctx, rec) -> tuple[int, str]:
    """Save each outcome and its diagnostics atomically; a failed item can be replayed.

    A per-payload lock serializes concurrent attempts. Parser errors roll back any
    partially written facts; interrupts propagate and leave the raw file retryable.
    """
    dq = DQLog()
    rows, error = 0, None
    with ctx.conn.transaction():
        ctx.conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                         (rec.content_sha256,))
        try:
            with ctx.conn.transaction():
                rows = load_document(ctx.conn, rec, ctx.store.read_bytes(rec), dq)
                loaded = ctx.conn.execute("""select exists(select 1 from filing
                    where exchange = %s and filing_system = %s and content_sha256 = %s)""",
                    (rec.request_params["exchange"], rec.request_params["filing_system"],
                     rec.content_sha256)).fetchone()[0]
                if not loaded:
                    error = "; ".join(i.message for i in dq.issues if i.severity == "error")
                    raise ValueError(error or "document produced no filing")
        except Exception as exc:  # noqa: BLE001 - persisted per-item failure, then continue
            rows, error = 0, f"{type(exc).__name__}: {exc}"
            if not any(i.severity == "error" for i in dq.issues):
                dq.emit("error", "document_processing_failed", error, fetch_id=rec.fetch_id)
        status = "failed" if error else "loaded"
        ctx.conn.execute("""insert into document_processing
            (fetch_id, status, parser_version, rows_loaded, last_error)
            values (%s, %s, %s, %s, %s)
            on conflict (fetch_id) do update set status = excluded.status,
                parser_version = excluded.parser_version, rows_loaded = excluded.rows_loaded,
                last_error = excluded.last_error, attempts = document_processing.attempts + 1,
                processed_at = now()""", (rec.fetch_id, status, parser_version(), rows, error))
        dq.persist(ctx.conn)
    # The context keeps summary counts, but these issues have already been persisted.
    ctx.dq.issues.extend(dq.issues)
    ctx.dq.mark_persisted(dq.issues)
    return rows, status


def replay_documents(ctx, filing_type: str, limit: int | None = None):
    """Retry downloaded documents that have no corresponding filing (including legacy loads)."""
    from igs.ingest.jobs import DOCUMENT_SOURCE, JobResult

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    records = ctx.conn.execute("""select p.fetch_id from raw_payload p
        where p.source_id = %s and p.http_status = 200
          and p.request_params->>'filing_type' = %s
          and not exists (select 1 from filing f where f.content_sha256 = p.content_sha256
              and f.exchange = p.request_params->>'exchange'
              and f.filing_system = p.request_params->>'filing_system')
        order by p.fetched_at, p.fetch_id limit %s""",
        (DOCUMENT_SOURCE, filing_type, limit)).fetchall()
    ctx.conn.commit()
    out = []
    for (fetch_id,) in records:
        rec = ctx.store.get(fetch_id)
        rows, status = process_document(ctx, rec)
        out.append(JobResult(DOCUMENT_SOURCE, rec.url, 200, rows, fetch_id, status))
    return out
