from __future__ import annotations

import datetime as dt
import os
import stat

import pytest
from synthetic import ist

from igs.ingest.raw_store import RawStore, RawStoreError


def test_roundtrip_and_metadata(tmp_path):
    store = RawStore(tmp_path)
    rec = store.put(source_id="nse_x", content=b"a,b\n1,2\n", url="https://e/x.csv",
                    http_status=200, content_type="text/csv", fetched_at=ist(2024, 5, 2, 19))
    assert store.read_bytes(rec.fetch_id) == b"a,b\n1,2\n"
    back = store.get(rec.fetch_id)
    assert back == rec
    assert back.fetched_at.tzinfo is not None
    assert rec.fetch_id.startswith("nse_x__20240502T133000")


def test_payloads_are_read_only(tmp_path):
    store = RawStore(tmp_path)
    rec = store.put(source_id="s", content=b"x", url=None, http_status=200)
    blob = tmp_path / rec.blob_path
    assert not (blob.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_identical_content_shares_one_blob_but_keeps_both_fetches(tmp_path):
    store = RawStore(tmp_path)
    a = store.put(source_id="s", content=b"same", url="u", http_status=200,
                  fetched_at=ist(2024, 1, 1, 10))
    b = store.put(source_id="s", content=b"same", url="u", http_status=200,
                  fetched_at=ist(2024, 1, 2, 10))
    assert a.blob_path == b.blob_path
    assert [r.fetch_id for r in store.iter_records()] == [a.fetch_id, b.fetch_id]


def test_fetch_records_are_never_overwritten(tmp_path):
    store = RawStore(tmp_path)
    ts = ist(2024, 1, 1, 10)
    store.put(source_id="s", content=b"x", url="u", http_status=200, fetched_at=ts)
    with pytest.raises(RawStoreError, match="already exists"):
        store.put(source_id="s", content=b"x", url="u", http_status=200, fetched_at=ts)


def test_corruption_is_detected(tmp_path):
    store = RawStore(tmp_path)
    rec = store.put(source_id="s", content=b"original", url="u", http_status=200)
    blob = tmp_path / rec.blob_path
    os.chmod(blob, 0o644)
    blob.write_bytes(b"tampered")
    with pytest.raises(RawStoreError, match="hash check"):
        store.read_bytes(rec)


def test_naive_timestamp_rejected(tmp_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        RawStore(tmp_path).put(source_id="s", content=b"x", url="u", http_status=200,
                               fetched_at=dt.datetime(2024, 1, 1))  # noqa: DTZ001


def test_manual_import(tmp_path):
    src = tmp_path / "screen.csv"
    src.write_bytes(b"Name,NSE Code\nFoo,FOO\n")
    store = RawStore(tmp_path / "raw")
    rec = store.put_file(source_id="screener_csv", path=src)
    assert rec.origin == "manual"
    assert rec.request_params == {"original_filename": "screen.csv"}
    assert store.read_bytes(rec) == src.read_bytes()


@pytest.mark.db
def test_reindex_rebuilds_index_idempotently(tmp_path, db_conn):
    store = RawStore(tmp_path)
    store.put(source_id="a", content=b"1", url="u1", http_status=200)
    store.put(source_id="b", content=b"2", url="u2", http_status=404)
    assert store.reindex_into_db(db_conn) == 2
    assert store.reindex_into_db(db_conn) == 0
    with db_conn.cursor() as cur:
        cur.execute("select source_id, http_status from raw_payload order by source_id")
        assert cur.fetchall() == [("a", 200), ("b", 404)]
