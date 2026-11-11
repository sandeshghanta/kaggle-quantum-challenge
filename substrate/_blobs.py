"""Content-addressed, zstd-compressed blob store. Digest is of the raw bytes."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

import zstandard

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class BlobNotFound(LookupError):
    """No blob with this digest exists here."""


class Blobs:
    def __init__(self, root: Path):
        self.dir = Path(root) / "blobs" / "sha256"

    def _path(self, hexdigest: str) -> Path:
        return self.dir / hexdigest[:2] / hexdigest[2:4] / hexdigest

    def put(self, data: bytes) -> str:
        h = hashlib.sha256(data).hexdigest()
        final = self._path(h)
        if not final.exists():
            final.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=final.parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(zstandard.ZstdCompressor(level=3).compress(data))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, final)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return f"sha256:{h}"

    def get(self, digest: str) -> bytes:
        m = _DIGEST.match(digest)
        if not m:
            raise ValueError(f"malformed digest: {digest!r}")
        path = self._path(m.group(1))
        if not path.exists():
            raise BlobNotFound(digest)
        return zstandard.ZstdDecompressor().decompress(path.read_bytes())
