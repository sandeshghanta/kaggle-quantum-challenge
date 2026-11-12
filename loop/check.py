"""Fast sample check of an invsqrt circuit: the lab assistant's `check` tool, and the worker's gate before it commits.

Builds `gen.py` (sandboxed) and simulates the circuit on a fixed sample of x: the domain edges, powers of two and
their neighbours, plus random values. Same pass/fail rules as the competition verifier (x restored, scratch clean,
phase within tolerance), and a cost estimate from the random part of the sample. Well under a second for a typical
circuit, against ~28 CPU-s for the verifier's full sweep. Passing here does not mean passing the verifier: the
verifier checks every x.
"""

from __future__ import annotations

import importlib
import json
import math
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from loop import config
from loop.programs import build

RANDOM_SAMPLES = 2048
SEED = 20261112           # fixed, so an assistant sees the same x values every time it checks
BUILD_TIMEOUT = 120


@dataclass(frozen=True)
class CheckReport:
    passes: bool
    stage: str             # "build" | "check" | "ok"
    error: str | None
    qubits: int | None = None
    toffolis: float | None = None
    cost_estimate: float | None = None
    samples: int = 0
    seconds: float = 0.0

    def text(self) -> str:
        if self.stage == "build":
            return f"BUILD FAILED\n{self.error}"
        head = "PASS" if self.passes else "FAIL"
        lines = [f"{head} on {self.samples} sampled x values ({self.seconds:.1f}s)"]
        if self.error:
            lines.append(f"first error: {self.error}")
        if self.qubits is not None:
            lines.append(f"qubits: {self.qubits}")
        if self.toffolis is not None:
            lines.append(f"Toffoli-equivalent per shot: {self.toffolis:,.1f}")
        if self.cost_estimate is not None:
            lines.append(f"estimated cost: {self.cost_estimate:,.0f}")
        return "\n".join(lines)


def _verifier():
    if str(config.VERIFIER_DIR) not in sys.path:
        sys.path.insert(0, str(config.VERIFIER_DIR))
    return importlib.import_module("invsqrt.verify_submission_invsqrt")


def sample_xs(max_x: int, *, n_random: int = RANDOM_SAMPLES, seed: int = SEED) -> tuple[np.ndarray, int]:
    """(xs, n_edge): edge cases first, then `n_random` uniform draws. Duplicates are fine."""
    edge = {1, 2, 3, 4, 5, max_x - 2, max_x - 1, max_x}
    p = 1
    while p <= max_x:
        edge |= {p - 1, p, p + 1}
        p *= 2
    r = 1
    while r * r <= max_x:  # perfect squares: 1/sqrt(x) is exact there
        edge.add(r * r)
        r = r * 2 + 1
    edge_xs = sorted(x for x in edge if 1 <= x <= max_x)
    rnd = np.random.default_rng(seed).integers(1, max_x + 1, size=n_random, dtype=np.uint64)
    return np.concatenate([np.array(edge_xs, dtype=np.uint64), rnd]), len(edge_xs)


def check_circuit(kmb: Path, *, n_random: int = RANDOM_SAMPLES) -> CheckReport:
    v = _verifier()
    km = v.km
    start = time.monotonic()
    spec = v.ProblemSpecInvSqrt.for_n_p(v.N_P)
    try:
        circuit = km.Circuit.from_file(str(kmb))
        x_reg, scratch = v.validate_registers(circuit, spec)
        xs, n_edge = sample_xs(spec.max_x, n_random=n_random)
        target, tol = v.reference_values(spec, xs)
        sim = km.Simulator(batch_size=len(xs), debug_print=False, counting_operations=True, seed=SEED)
        sim.clear_for_shot()
        sim.write_across_shots(x_reg, v.to_bits(xs, spec.width))
        sim.do(circuit)
    except Exception as e:  # malformed circuit, wrong registers, simulator error
        return CheckReport(False, "check", f"{type(e).__name__}: {e}"[:2000], seconds=time.monotonic() - start)

    def report(error: str | None, **kw) -> CheckReport:
        return CheckReport(error is None, "ok" if error is None else "check", error, qubits=circuit.num_qubits,
                           samples=len(xs), seconds=round(time.monotonic() - start, 2), **kw)

    if not np.array_equal(v.from_bits(sim.read_across_shots(x_reg)), xs):
        bad = int(np.flatnonzero(v.from_bits(sim.read_across_shots(x_reg)) != xs)[0])
        return report(f"x={int(xs[bad])}: the x register was not restored")
    if len(scratch):
        dirty = sim.read_across_shots(scratch).any(axis=1)
        if dirty.any():
            return report(f"scratch qubit {scratch[int(np.argmax(dirty))]} did not return to |0>")
    theta = np.array([v.half_turns_to_units(sim.read_shot_phase(i)) for i in range(len(xs))], dtype=np.uint64)
    error = (theta - target).view(np.int64)
    used = np.abs(error).view(np.uint64)
    bad = np.flatnonzero(used > tol)
    if len(bad):
        i = int(bad[0])
        return report(f"x={int(xs[i])}: phase error {error[i] / 2**64:.3e} turns exceeds tolerance "
                      f"{tol[i] / 2**64:.3e}")

    # Cost, as the verifier computes it, but averaged over the random part only (edges would bias it).
    stats = sim.operation_statistics()
    n = len(xs)
    toffoli = stats.num_toffoli_like / n
    t_base = stats.num_t_like / n
    n_rot = stats.num_z_pow(min_clifford_hierarchy_level=4) / n
    margin = int((tol - used).min())
    eps = math.pi * margin / 2**64 / n_rot if n_rot else None
    t_count = t_base + (n_rot * v.rotation_t_count(eps) if n_rot and eps and eps > 0 else 0)
    toffolis = toffoli + t_count / v.T_PER_TOFFOLI
    del n_edge  # counts are per batch, so the edge share is folded into the average; fine for an estimate
    return report(None, toffolis=round(toffolis, 2), cost_estimate=round(circuit.num_qubits * toffolis, 1))


def check_bundle(bundle: Path, *, n_random: int = RANDOM_SAMPLES) -> CheckReport:
    """Build the bundle's gen.py into a temporary file, then sample-check the circuit."""
    with tempfile.TemporaryDirectory(prefix="check-") as tmp:
        out = Path(tmp) / "circuit.kmb"
        built = build(Path(bundle), out, timeout=BUILD_TIMEOUT)
        if not built.ok:
            err = (built.stderr or "gen.py exited without writing a circuit").strip()
            return CheckReport(False, "build", err[-2000:])
        return check_circuit(built.kmb, n_random=n_random)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m loop.check <bundle-dir> [--json]", file=sys.stderr)
        return 2
    r = check_bundle(Path(args[0]))
    print(json.dumps(asdict(r)) if "--json" in args else r.text())
    return 0 if r.passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
