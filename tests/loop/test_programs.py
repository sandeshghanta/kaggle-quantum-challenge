import io
import os
import tarfile
import time

import pytest

from loop.programs import build, pack, unpack


def bundle(tmp_path, gen: str, extra: dict | None = None):
    d = tmp_path / "b"
    d.mkdir(exist_ok=True)
    (d / "gen.py").write_text(gen)
    for name, text in (extra or {}).items():
        (d / name).write_text(text)
    return d


WRITE = "import sys\nopen(sys.argv[1], 'wb').write(b'circuit')\n"


def test_pack_is_deterministic_and_ignores_mtime(tmp_path):
    d = bundle(tmp_path, WRITE, {"helper.py": "x = 1\n"})
    first = pack(d)
    os.utime(d / "gen.py", (time.time() + 100, time.time() + 100))
    assert pack(d) == first
    (d / "helper.py").write_text("x = 2\n")
    assert pack(d) != first


def test_pack_requires_gen_py(tmp_path):
    (tmp_path / "x").mkdir()
    with pytest.raises(ValueError):
        pack(tmp_path / "x")


def test_unpack_roundtrips(tmp_path):
    d = bundle(tmp_path, WRITE, {"sub/mod.py": "y = 3\n"} if False else {"mod.py": "y = 3\n"})
    out = tmp_path / "out"
    unpack(pack(d), out)
    assert (out / "gen.py").read_text() == WRITE and (out / "mod.py").read_text() == "y = 3\n"


def test_unpack_rejects_path_traversal(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("../evil")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        unpack(buf.getvalue(), tmp_path / "out")


def test_build_ok(tmp_path):
    r = build(bundle(tmp_path, WRITE), tmp_path / "c.kmb")
    assert r.ok and r.kmb == tmp_path / "c.kmb" and r.kmb.read_bytes() == b"circuit"


def test_build_timeout_kills(tmp_path):
    r = build(bundle(tmp_path, "import time\ntime.sleep(10)\n"), tmp_path / "c.kmb", timeout=0.5)
    assert not r.ok and "timed out" in r.stderr and r.seconds < 5


def test_build_nonzero_exit_fails(tmp_path):
    r = build(bundle(tmp_path, "raise SystemExit(3)\n"), tmp_path / "c.kmb")
    assert not r.ok and r.kmb is None


def test_build_without_output_fails(tmp_path):
    r = build(bundle(tmp_path, "pass\n"), tmp_path / "c.kmb")
    assert not r.ok and r.kmb is None


def test_pack_ignores_symlinks(tmp_path):  # security review I2
    secret = tmp_path / "secret.txt"
    secret.write_text("api-key")
    d = bundle(tmp_path, WRITE)
    (d / "leak").symlink_to(secret)
    out = tmp_path / "out"
    unpack(pack(d), out)
    assert not (out / "leak").exists()


def test_build_rejects_symlinked_output(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("api-key")
    d = bundle(tmp_path, f"import os, sys\nos.symlink({str(secret)!r}, sys.argv[1])\n")
    r = build(d, d / "c.kmb")
    assert not r.ok and r.kmb is None and "symlink" in r.stderr
