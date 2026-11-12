"""Quick and full verification of invsqrt circuits, and recording the outcome."""

from __future__ import annotations

import json
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from loop import config
from loop.programs import build, unpack
from substrate.cli import Substrate

BUILD_TIMEOUT = 120
QUICK_MAX_X = 65536
VERSION = "v1"


@dataclass(frozen=True)
class CheckResult:
    passes: bool
    cost: float | None
    metrics: dict
    shots: int
    seeds: list
    error: str | None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    kind: str  # "rejected" | "measured"
    event_id: str
    cost: float | None


def _run_verifier(kmb: Path, extra: list[str]) -> CheckResult:
    start = time.monotonic()
    proc = subprocess.run([sys.executable, "-I", "invsqrt/verify_submission_invsqrt.py", str(kmb), *extra],
                          cwd=config.VERIFIER_DIR, capture_output=True, text=True)
    elapsed = time.monotonic() - start
    try:
        raw = json.loads(proc.stdout[proc.stdout.index("{"):])
    except ValueError:
        raw = {"status": "FAIL", "error": (proc.stderr or proc.stdout).strip()[-2000:]}
    ok = raw.get("status") == "PASS"
    metrics = {"seconds": round(elapsed, 2)}
    if "num_qubits" in raw:
        metrics |= {"Q": raw["num_qubits"], "T": raw.get("toffoli_count"),
                    "toffoli_equivalent": raw.get("toffoli_equivalent")}
    return CheckResult(passes=ok, cost=raw.get("score") if ok else None, metrics=metrics,
                       shots=int(raw.get("verified_shots", 0)), seeds=[],
                       error=None if ok else str(raw.get("error", "verifier failed")), raw=raw)


def _workers(max_workers: int | None) -> list[str]:
    return ["--max-workers", str(max_workers)] if max_workers else []


def quick_check(kmb: Path, *, max_workers: int | None = None) -> CheckResult:
    return _run_verifier(kmb, ["--max-x", str(QUICK_MAX_X), *_workers(max_workers)])


def full_check(kmb: Path, *, salts: list[int], max_workers: int | None = None,
               max_x: int | None = None) -> CheckResult:
    extra = _workers(max_workers) + (["--max-x", str(max_x)] if max_x else [])
    results = [_run_verifier(kmb, ["--salt", str(s), *extra]) for s in salts]
    passing = [r for r in results if r.passes]
    worst = max(passing, key=lambda r: r.cost) if passing else None
    failed = next((r for r in results if not r.passes), None)
    return CheckResult(
        passes=failed is None and bool(results),
        cost=worst.cost if worst else None,
        metrics=(worst or results[0]).metrics if results else {},
        shots=sum(r.shots for r in results),
        seeds=list(salts),
        error=failed.error if failed else None,
        raw={"per_salt": [r.raw for r in results]},
    )


def evaluate(sub: Substrate, program: str, *, island_best_quick: float | None, salts: list[int],
             max_workers: int | None, workdir: Path, cause: str, full_max_x: int | None = None) -> Outcome:
    actor = config.actor("verifier", VERSION)
    subject = "prog:" + program
    cpu0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()

    def spend() -> None:  # written before the result, so a kill in between cannot lose the CPU spend
        cpu1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime)
        sub.append("budget.spent", {"pool": "cpu", "tokens_in": 0, "tokens_out": 0, "cpu_seconds": round(cpu, 2),
                                    "wall_seconds": round(time.monotonic() - t0, 2)},
                   subject="pool:cpu", actor=actor, cause=cause)

    def reject(reason: str, detail: str, circuit: str | None = None, cost: float | None = None) -> Outcome:
        spend()
        eid = sub.append("candidate.rejected", {"program": program, "circuit": circuit,
                                                "reason": reason, "detail": detail[-4000:]},
                         subject=subject, actor=actor, cause=cause)
        return Outcome("rejected", eid, cost)

    shutil.rmtree(workdir, ignore_errors=True)
    unpack(sub.get_blob(program), workdir)
    built = build(workdir, workdir / "circuit.kmb", timeout=BUILD_TIMEOUT)
    if not built.ok:
        outcome = reject("build_failed", built.stderr or "generator exited without writing a circuit")
    else:
        circuit = sub.put_blob(built.kmb.read_bytes())
        already = _measured_circuit(sub, circuit)
        if already is not None:
            return reject("duplicate", f"circuit identical to one already measured (program {already})", circuit)
        quick = quick_check(built.kmb, max_workers=max_workers)
        if not quick.passes:
            outcome = reject("quick_check_failed", quick.error or "", circuit)
        elif island_best_quick is not None and quick.cost >= island_best_quick:
            outcome = reject("not_better",
                             f"quick cost {quick.cost:.6g} >= island best quick cost {island_best_quick:.6g}",
                             circuit, quick.cost)
        else:
            full = full_check(built.kmb, salts=salts, max_workers=max_workers, max_x=full_max_x)
            cost = full.cost if full.cost is not None else quick.cost
            payload = {"program": program, "circuit": circuit, "cost": cost, "passes": full.passes,
                       "benchmark": config.BENCHMARK, "metrics": full.metrics | {"quick_cost": quick.cost},
                       "shots": full.shots,
                       "seeds": full.seeds, "verifier": config.verifier_tag(), "cost_basis": config.COST_BASIS}
            if not full.passes:
                payload["failure"] = {"detail": (full.error or "")[-2000:]}
            spend()
            eid = sub.append("candidate.measured", payload, subject=subject, actor=actor, cause=cause)
            outcome = Outcome("measured", eid, cost)
    return outcome


def _measured_circuit(sub: Substrate, circuit: str) -> str | None:
    """Program of an existing measurement of this exact circuit, if any."""
    for ev in sub.since(None, types=["candidate.measured"], limit=-1)[0]:
        if ev.payload.get("circuit") == circuit:
            return ev.payload["program"]
    return None
