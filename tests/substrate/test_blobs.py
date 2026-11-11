import hashlib

import pytest
import zstandard

from substrate._blobs import BlobNotFound, Blobs


def test_put_returns_digest_of_raw_bytes_and_roundtrips(tmp_path):
    b = Blobs(tmp_path)
    data = b"circuit" * 1000
    d = b.put(data)
    h = hashlib.sha256(data).hexdigest()
    assert d == f"sha256:{h}" and b.get(d) == data
    stored = (tmp_path / "blobs/sha256" / h[:2] / h[2:4] / h).read_bytes()
    assert stored != data and zstandard.decompress(stored) == data


def test_put_is_idempotent(tmp_path):
    b = Blobs(tmp_path)
    assert b.put(b"x") == b.put(b"x")


def test_missing_blob_raises(tmp_path):  # Review Focus 2
    with pytest.raises(BlobNotFound):
        Blobs(tmp_path).get("sha256:" + "0" * 64)


def test_malformed_digest_raises(tmp_path):
    with pytest.raises(ValueError):
        Blobs(tmp_path).get("md5:abc")


def test_no_temp_files_left_and_partial_never_at_final_path(tmp_path):
    b = Blobs(tmp_path)
    b.put(b"y" * 10_000)
    files = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(files) == 1 and not files[0].name.startswith(".")
