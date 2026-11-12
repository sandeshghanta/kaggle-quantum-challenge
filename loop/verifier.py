"""Verifier service: evaluates every newly committed program, once, in commit order."""

from __future__ import annotations

import argparse
import signal
import time
from pathlib import Path

from loop import pool
from loop.state import load_cursor, save_cursor
from loop.verify import evaluate
from substrate.cli import Substrate
from substrate.types import Event

JUDGED = ["candidate.measured", "candidate.rejected"]


def island_best(sub: Substrate, program: str) -> float | None:
    """Best passing cost in the program's island, or None if unknown."""
    island = pool.island_of(sub, program)
    return pool.island_best(sub, island) if island else None


def pending(sub: Substrate, cursor: int) -> tuple[list[Event], int]:
    commits, new_cursor = sub.since(cursor, types=["program.committed"], limit=-1)
    todo = [ev for ev in commits if not sub.history("prog:" + ev.payload["blob"], types=JUDGED)]
    return todo, new_cursor


def run_once(sub: Substrate, *, state_path: Path, workdir: Path, salts: list[int],
             max_workers: int | None, full_max_x: int | None = None, stop=lambda: False) -> int:
    cursor = load_cursor(state_path)
    todo, new_cursor = pending(sub, cursor)
    done = 0
    for ev in todo:
        if stop():
            return done
        program = ev.payload["blob"]
        evaluate(sub, program, island_best=island_best(sub, program), salts=salts,
                 max_workers=max_workers, workdir=workdir, cause=ev.id, full_max_x=full_max_x)
        save_cursor(state_path, ev.seq)
        done += 1
    if not stop():
        save_cursor(state_path, new_cursor)
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--salts", type=int, nargs="+", default=[1, 2, 3, 4])
    p.add_argument("--max-workers", type=int)
    p.add_argument("--poll", type=float, default=5.0)
    args = p.parse_args(argv)

    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True
        print("verifier: stop requested; finishing current job", flush=True)

    signal.signal(signal.SIGTERM, on_term)
    sub = Substrate(args.root)
    while not stopping:
        n = run_once(sub, state_path=Path(args.state), workdir=Path(args.workdir), salts=args.salts,
                     max_workers=args.max_workers, stop=lambda: stopping)
        if n:
            print(f"verifier: evaluated {n} program(s)", flush=True)
        for _ in range(int(args.poll * 10)):
            if stopping:
                break
            time.sleep(0.1)
    print("verifier: finished current job, exiting", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
