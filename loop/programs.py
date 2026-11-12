"""Program bundles: a directory with entry gen.py, stored as one deterministic tar blob."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from loop import sandbox

STDERR_LIMIT = 4000


def _files(bundle_dir: Path) -> list[Path]:
    skip = {"__pycache__", ".pytest_cache"}
    return sorted(p for p in bundle_dir.rglob("*")
                  if p.is_file() and not (skip & set(p.relative_to(bundle_dir).parts)))


def pack(bundle_dir: Path) -> bytes:
    bundle_dir = Path(bundle_dir)
    if not (bundle_dir / "gen.py").is_file():
        raise ValueError(f"{bundle_dir} has no gen.py")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for path in _files(bundle_dir):
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(bundle_dir).as_posix())
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(data: bytes, dest: Path) -> None:
    dest = Path(dest)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tf:
        members = tf.getmembers()
        for m in members:
            p = PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts or not m.isfile():
                raise ValueError(f"unsafe bundle member: {m.name!r}")
        dest.mkdir(parents=True, exist_ok=True)
        for m in members:
            target = dest / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tf.extractfile(m).read())


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    kmb: Path | None
    stderr: str
    seconds: float


def build(bundle_dir: Path, out_kmb: Path, *, timeout: float = 120) -> BuildResult:
    out_kmb = Path(out_kmb)
    out_kmb.unlink(missing_ok=True)
    start = time.monotonic()
    cmd = sandbox.wrap([sys.executable, "-I", "gen.py", str(out_kmb)],
                       rw=sorted({Path(bundle_dir), out_kmb.parent}), ro=sandbox.python_paths(), network=False)
    proc = subprocess.Popen(cmd, cwd=bundle_dir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        _, stderr = proc.communicate()
        stderr = (stderr or "") + f"\nbuild timed out after {timeout}s"
        return BuildResult(False, None, stderr[-STDERR_LIMIT:], time.monotonic() - start)
    ok = proc.returncode == 0 and out_kmb.exists()
    return BuildResult(ok, out_kmb if ok else None, (stderr or "")[-STDERR_LIMIT:], time.monotonic() - start)
