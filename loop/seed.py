"""Seed the pool with Google's reference generator (1x). Idempotent."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from loop import config
from loop.programs import pack
from substrate.cli import Substrate

ROLE_DIR = config.REPO / "roles" / "lab_assistant" / "v1"


def seed_reference(sub: Substrate, *, island: str = "main") -> str:
    actor = config.actor("seed", "v1")
    if island not in {ev.payload["island"] for ev in sub.history("bench:invsqrt", types=["island.created"])}:
        sub.append("island.created", {"island": island, "idea": "Newton-Raphson reference (paper baseline)",
                                      "instructions": (ROLE_DIR / f"island_{island}.md").read_text().strip(),
                                      "weight": 1.0}, subject="bench:invsqrt", actor=actor)
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(config.VERIFIER_DIR / "invsqrt" / "nr_reference_circuit.py", Path(d) / "gen.py")
        program = sub.put_blob(pack(Path(d)))
    subject = "prog:" + program
    if not sub.history(subject, types=["program.committed"]):
        sub.append("program.committed", {"blob": program, "parent": None, "benchmark": config.BENCHMARK,
                                         "note": "Google reference generator (1x)"}, subject=subject, actor=actor)
    if not any(ev.payload["island"] == island for ev in sub.history(subject, types=["island.joined"])):
        sub.append("island.joined", {"program": program, "island": island, "via": "seed"},
                   subject=subject, actor=actor)
    return program


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    args = p.parse_args(argv)
    sub = Substrate(args.root)
    sub.init()
    print(seed_reference(sub))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
