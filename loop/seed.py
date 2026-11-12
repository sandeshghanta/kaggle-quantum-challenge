"""Seed the pool: Google's reference generator (island main) and the Marsyas generator (island marsyas). Idempotent."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from loop import config
from loop.programs import pack
from substrate import connect
from substrate.cli import Substrate

ROLE_DIR = config.REPO / "roles" / "lab_assistant" / "v1"
MARSYAS_DIR = config.REPO / "seeds" / "marsyas_invsqrt"


def seed_bundle(sub: Substrate, bundle_dir: Path, *, island: str, idea: str, instructions: str, note: str) -> str:
    """Pack bundle_dir, commit it as a root program and join it to `island`. Idempotent."""
    actor = config.actor("seed", "v1")
    if island not in {ev.payload["island"] for ev in sub.history("bench:invsqrt", types=["island.created"])}:
        sub.append("island.created", {"island": island, "idea": idea, "instructions": instructions.strip(),
                                      "weight": 1.0}, subject="bench:invsqrt", actor=actor)
    program = sub.put_blob(pack(Path(bundle_dir)))
    subject = "prog:" + program
    if not sub.history(subject, types=["program.committed"]):
        sub.append("program.committed", {"blob": program, "parent": None, "benchmark": config.BENCHMARK,
                                         "note": note}, subject=subject, actor=actor)
    if not any(ev.payload["island"] == island for ev in sub.history(subject, types=["island.joined"])):
        sub.append("island.joined", {"program": program, "island": island, "via": "seed"},
                   subject=subject, actor=actor)
    return program


def seed_reference(sub: Substrate, *, island: str = "main") -> str:
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(config.VERIFIER_DIR / "invsqrt" / "nr_reference_circuit.py", Path(d) / "gen.py")
        return seed_bundle(sub, Path(d), island=island, idea="Newton-Raphson reference (paper baseline)",
                           instructions=(ROLE_DIR / f"island_{island}.md").read_text(),
                           note="Google reference generator (1x)")


def seed_marsyas(sub: Substrate, island: str = "marsyas") -> str:
    return seed_bundle(sub, MARSYAS_DIR, island=island,
                       idea="Marsyas signed-digit Horner generator (public Kaggle notebook, ~13.9x)",
                       instructions=(ROLE_DIR / f"island_{island}.md").read_text(),
                       note="Marsyas vqcc-invsqrt-13-94x-full-source (Apache-2.0), imported unchanged in behaviour")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--substrate", "--root", dest="substrate", required=True,
                   help="data directory (in-process) or unix:/run/substrate/api.sock")
    args = p.parse_args(argv)
    sub = connect(args.substrate)
    if isinstance(sub, Substrate):  # substrated registers schemas itself at startup
        sub.init()
    print(seed_reference(sub))
    print(seed_marsyas(sub))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
