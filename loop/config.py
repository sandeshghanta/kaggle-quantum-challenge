"""Loop-wide constants and provenance helpers."""

from __future__ import annotations

import functools
import hashlib
import socket
import subprocess
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERIFIER_DIR = REPO / "verifiers"
BENCHMARK = "invsqrt"
COST_BASIS = "invsqrt/QxC/v1"


@functools.cache
def verifier_tag() -> str:
    digest = hashlib.sha256((VERIFIER_DIR / "invsqrt" / "verify_submission_invsqrt.py").read_bytes()).hexdigest()
    return f"kickmix@{metadata.version('kickmix')}+invsqrt@{digest[:12]}"


@functools.cache
def git_sha() -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "nogit"
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def actor(role: str, version: str, model: str | None = None) -> dict:
    a = {"role": role, "harness": f"{role}/{version}@{git_sha()}", "host": socket.gethostname()}
    if model is not None:
        a["model"] = model
    return a
