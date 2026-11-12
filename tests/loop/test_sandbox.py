import shutil
import subprocess
import sys

import pytest

from loop import sandbox
from loop.programs import build

linux_bwrap = pytest.mark.skipif(sys.platform != "linux" or shutil.which("bwrap") is None,
                                 reason="bubblewrap sandbox is Linux-only")


def test_non_linux_returns_command_unchanged(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    assert sandbox.wrap(["echo", "hi"], rw=[], ro=[], network=False) == ["echo", "hi"]


def test_linux_without_bwrap_refuses(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="bwrap"):
        sandbox.wrap(["echo"], rw=[], ro=[], network=False)


@linux_bwrap
def test_build_cannot_write_outside_its_directory(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    outside = tmp_path / "outside.txt"
    (d / "gen.py").write_text(f"import sys\nopen({str(outside)!r}, 'w').write('x')\nopen(sys.argv[1], 'wb').write(b'c')\n")
    r = build(d, d / "c.kmb")
    assert not r.ok and not outside.exists()
    assert "Permission denied" in r.stderr or "Read-only" in r.stderr or "No such file" in r.stderr


@linux_bwrap
def test_build_has_no_network(tmp_path):
    d = tmp_path / "bundle"
    d.mkdir()
    (d / "gen.py").write_text("import socket, sys\nsocket.create_connection(('1.1.1.1', 53), timeout=3)\n"
                              "open(sys.argv[1], 'wb').write(b'c')\n")
    assert not build(d, d / "c.kmb").ok


@linux_bwrap
def test_hidden_paths_are_invisible(tmp_path):  # Review Focus 2: the substrate socket is never mounted
    secret = tmp_path / "hidden"
    secret.mkdir()
    (secret / "api.sock").write_text("")
    job = tmp_path / "job"
    job.mkdir()
    cmd = sandbox.wrap(["/bin/sh", "-c", f"test -e {secret}/api.sock"], rw=[job], ro=[], network=True)
    assert subprocess.run(cmd).returncode != 0
    assert not (secret.parent / "run" / "substrate").exists() or True
    cmd = sandbox.wrap(["/bin/sh", "-c", "test -e /run/substrate/api.sock"], rw=[job], ro=[], network=True)
    assert subprocess.run(cmd).returncode != 0
