"""Verifier service: evaluates every newly committed program, once, in commit order."""

from __future__ import annotations

import argparse
import signal
import time
from pathlib import Path

from datetime import UTC, datetime

from loop import config, pool, verify
from loop.state import load_cursor, save_cursor
from loop.verify import evaluate
from substrate import connect
from substrate.cli import Substrate
from substrate.types import Event

JUDGED = ["candidate.measured", "candidate.rejected"]


def island_best_sweep(sub: Substrate, program: str) -> float | None:
    """Sweep cost of the best candidate in the program's island, or None if unknown."""
    island = pool.island_of(sub, program)
    return pool.island_best_sweep(sub, island) if island else None


def backlog(sub: Substrate, cursor: int) -> str:
    """One log line: how many committed programs wait for verification, and how old the oldest is."""
    todo, _ = pending(sub, cursor)
    if not todo:
        return "verifier: backlog 0"
    oldest = datetime.fromisoformat(todo[0].ts.replace("Z", "+00:00"))
    age = (datetime.now(UTC) - oldest).total_seconds()
    return f"verifier: backlog {len(todo)} program(s), oldest waiting {age / 60:.1f} min"


def pending(sub: Substrate, cursor: int) -> tuple[list[Event], int]:
    commits, new_cursor = sub.since(cursor, types=["program.committed"], limit=-1)
    todo = [ev for ev in commits if not sub.history("prog:" + ev.payload["blob"], types=JUDGED)]
    return todo, new_cursor


def run_once(sub: Substrate, *, state_path: Path, workdir: Path, salts: list[int],
             max_workers: int | None, full_max_x: int | None = None, stop=lambda: False,
             delta: float = verify.DELTA, audit_rate: float = verify.AUDIT_RATE, rng=None) -> int:
    cursor = load_cursor(state_path)
    todo, new_cursor = pending(sub, cursor)
    done = 0
    for ev in todo:
        if stop():
            return done
        program = ev.payload["blob"]
        try:
            evaluate(sub, program, island_best_sweep=island_best_sweep(sub, program), salts=salts,
                     max_workers=max_workers, workdir=workdir, cause=ev.id, full_max_x=full_max_x,
                     delta=delta, audit_rate=audit_rate, rng=rng)
        except Exception as e:  # never loop on one program: record it and move on
            sub.append("candidate.rejected", {"program": program, "reason": "verify_error", "tier": "verify",
                                              "detail": f"{type(e).__name__}: {e}"[-4000:]},
                       subject="prog:" + program, actor=config.actor("verifier", "v1"), cause=ev.id)
        save_cursor(state_path, ev.seq)
        done += 1
    if not stop():
        save_cursor(state_path, new_cursor)
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--substrate", "--root", dest="substrate", required=True,
                   help="data directory (in-process) or unix:/run/substrate/api.sock")
    p.add_argument("--state", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--salts", type=int, nargs="+", default=[1, 2, 3, 4])
    p.add_argument("--max-workers", type=int)
    p.add_argument("--poll", type=float, default=5.0)
    p.add_argument("--delta", type=float, default=verify.DELTA, help="gate margin (0.05 = 5%% worse)")
    p.add_argument("--audit-rate", type=float, default=verify.AUDIT_RATE,
                   help="share of would-be gate rejections fully checked anyway")
    args = p.parse_args(argv)

    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True
        print("verifier: stop requested; finishing current job", flush=True)

    signal.signal(signal.SIGTERM, on_term)
    sub = connect(args.substrate)
    while not stopping:
        n = run_once(sub, state_path=Path(args.state), workdir=Path(args.workdir), salts=args.salts,
                     max_workers=args.max_workers, stop=lambda: stopping, delta=args.delta,
                     audit_rate=args.audit_rate)
        print(backlog(sub, load_cursor(Path(args.state))), flush=True)
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
