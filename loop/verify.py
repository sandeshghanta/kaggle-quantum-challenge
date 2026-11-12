"""Tiered verification of invsqrt circuits, and recording the outcome.

Tiers: build (sandboxed) -> duplicate check -> sweep (every x, 1 shot; validates and estimates cost)
-> gate (not_better only if the estimate is more than DELTA worse than the island's best; a random
AUDIT_RATE of would-be rejections are fully checked anyway) -> full check (64 shots per x), salt by
salt, stopping at the first failing salt.
"""

from __future__ import annotations

import json
import random
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
SWEEP_TIMEOUT = 1800          # a sane circuit takes seconds; a gate-count blow-up must not hang the verifier
FULL_TIMEOUT_PER_SALT = 4 * 3600
SWEEP_BATCH = 292             # 3,133,452 = 2^2 * 3 * 7^2 * 73^2 shots: the default batch (256) does not divide it
DELTA = 0.05
AUDIT_RATE = 0.05
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


def _run_verifier(kmb: Path, extra: list[str], timeout: float) -> CheckResult:
    start = time.monotonic()
    try:
        proc = subprocess.run([sys.executable, "-I", "invsqrt/verify_submission_invsqrt.py", str(kmb), *extra],
                              cwd=config.VERIFIER_DIR, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(passes=False, cost=None, metrics={"seconds": round(time.monotonic() - start, 2)},
                           shots=0, seeds=[], error=f"verifier timed out after {timeout}s")
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


def sweep_check(kmb: Path, *, max_workers: int | None = None) -> CheckResult:
    """Every x in the domain, one shot each: validates the whole domain and estimates the cost."""
    return _run_verifier(kmb, ["--shots-per-x", "1", "--batch-size", str(SWEEP_BATCH), *_workers(max_workers)],
                         SWEEP_TIMEOUT)


def full_check(kmb: Path, *, salts: list[int], max_workers: int | None = None,
               max_x: int | None = None) -> CheckResult:
    extra = _workers(max_workers) + (["--max-x", str(max_x)] if max_x else [])
    results = []
    for s in salts:  # stop at the first failing salt: one failure already proves the circuit wrong
        results.append(_run_verifier(kmb, ["--salt", str(s), *extra], FULL_TIMEOUT_PER_SALT))
        if not results[-1].passes:
            break
    passing = [r for r in results if r.passes]
    worst = max(passing, key=lambda r: r.cost) if passing else None
    failed = next((r for r in results if not r.passes), None)
    return CheckResult(
        passes=failed is None and bool(results),
        cost=worst.cost if worst else None,
        metrics=(worst or results[0]).metrics if results else {},
        shots=sum(r.shots for r in results),
        seeds=list(salts[:len(results)]),
        error=failed.error if failed else None,
        raw={"per_salt": [r.raw for r in results]},
    )


def evaluate(sub: Substrate, program: str, *, island_best_sweep: float | None, salts: list[int],
             max_workers: int | None, workdir: Path, cause: str, full_max_x: int | None = None,
             delta: float = DELTA, audit_rate: float = AUDIT_RATE, rng: random.Random | None = None) -> Outcome:
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

    rng = rng or random.Random()

    def reject(reason: str, detail: str, tier: str, circuit: str | None = None,
               estimate: CheckResult | None = None) -> Outcome:
        spend()
        payload = {"program": program, "circuit": circuit, "reason": reason, "detail": detail[-4000:], "tier": tier}
        if estimate is not None and estimate.cost is not None:
            payload["estimate"] = {"cost": estimate.cost, "Q": estimate.metrics.get("Q"), "T": estimate.metrics.get("T")}
        eid = sub.append("candidate.rejected", payload, subject=subject, actor=actor, cause=cause)
        return Outcome("rejected", eid, estimate.cost if estimate else None)

    shutil.rmtree(workdir, ignore_errors=True)
    unpack(sub.get_blob(program), workdir)
    built = build(workdir, workdir / "circuit.kmb", timeout=BUILD_TIMEOUT)
    if not built.ok:
        outcome = reject("build_failed", built.stderr or "generator exited without writing a circuit", "build")
    else:
        circuit = sub.put_blob(built.kmb.read_bytes())
        already = _measured_circuit(sub, circuit)
        if already is not None:
            return reject("duplicate", f"circuit identical to one already measured (program {already})",
                          "duplicate", circuit)
        sweep = sweep_check(built.kmb, max_workers=max_workers)
        worse = (island_best_sweep is not None and sweep.passes
                 and sweep.cost > island_best_sweep * (1 + delta))
        audited = worse and rng.random() < audit_rate
        if not sweep.passes:
            outcome = reject("quick_check_failed", sweep.error or "", "sweep", circuit, sweep)
        elif worse and not audited:
            outcome = reject("not_better",
                             f"sweep estimate {sweep.cost:.6g} > island best {island_best_sweep:.6g} x {1 + delta}",
                             "gate", circuit, sweep)
        else:
            full = full_check(built.kmb, salts=salts, max_workers=max_workers, max_x=full_max_x)
            cost = full.cost if full.cost is not None else sweep.cost
            metrics = full.metrics | {"sweep_cost": sweep.cost}
            if audited:
                metrics["audited"] = True  # a would-be gate rejection, checked to measure the gate's error
            payload = {"program": program, "circuit": circuit, "cost": cost, "passes": full.passes,
                       "benchmark": config.BENCHMARK, "metrics": metrics,
                       "shots": full.shots,
                       "seeds": full.seeds, "verifier": config.verifier_tag(), "cost_basis": config.COST_BASIS}
            if not full.passes:
                payload["failure"] = {"detail": (full.error or "")[-2000:]}
            if not payload["shots"]:
                del payload["shots"]  # the verifier reports no shot count on failure; the field is optional
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
