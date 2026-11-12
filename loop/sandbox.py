"""Confine model-written code (pi jobs, generator builds) with bubblewrap.

Inside the sandbox a process sees only: the Nix store and system dirs read-only, the paths listed in
`ro` read-only, the paths in `rw` writable, a private /tmp and HOME, and (optionally) the network.
Nothing else exists, in particular /run (so the substrate socket) and /var/lib (so the event log).

On Linux bwrap is required: running unconfined by accident is worse than refusing to start. Other
platforms (macOS development) run the command unchanged and warn once.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)
_warned = False

SYSTEM_RO = ["/nix/store", "/usr", "/bin", "/lib", "/lib64", "/etc", "/run/current-system/sw"]


def wrap(cmd: list[str], *, rw: list[Path], ro: list[Path], network: bool) -> list[str]:
    global _warned
    if sys.platform != "linux":
        if not _warned:
            log.warning("bubblewrap is Linux-only; running model-written code unsandboxed on %s", sys.platform)
            _warned = True
        return list(cmd)
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("bwrap (bubblewrap) is required on Linux to sandbox model-written code")
    args = [bwrap, "--die-with-parent", "--unshare-all", "--new-session"]
    if network:
        args.append("--share-net")
    for path in SYSTEM_RO:
        args += ["--ro-bind-try", path, path]
    for path in ro:
        args += ["--ro-bind", str(Path(path).resolve()), str(Path(path).resolve())]
    for path in rw:
        args += ["--bind", str(Path(path).resolve()), str(Path(path).resolve())]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--setenv", "HOME", "/tmp"]
    if rw:
        args += ["--chdir", str(Path(rw[0]).resolve())]
    return args + ["--", *cmd]


def python_paths() -> list[Path]:
    """Read-only paths a sandboxed Python needs: the venv (and its site-packages) and the interpreter."""
    paths = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(sys.executable).resolve().parent}
    return sorted(p for p in paths if p.exists())
