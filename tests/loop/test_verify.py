import pytest

from loop import config, verify
from loop.programs import pack
from substrate.cli import Substrate

REFERENCE = (config.VERIFIER_DIR / "invsqrt" / "nr_reference_circuit.py").read_text()

WRONG = """import sys
import kickmix as km
b = km.CircuitBuilder()
b.create_quantum_register(22, name='x')
b.finish_circuit().to_file(sys.argv[1], format='kmb')
"""


@pytest.fixture
def sub(tmp_path):
    return Substrate(tmp_path / "s")


def commit_bundle(sub, tmp_path, gen: str) -> tuple[str, str]:
    d = tmp_path / "bundle"
    d.mkdir(exist_ok=True)
    (d / "gen.py").write_text(gen)
    blob = sub.put_blob(pack(d))
    eid = sub.append("program.committed", {"blob": blob, "parent": None, "benchmark": "invsqrt"},
                     subject="prog:" + blob, actor={"role": "test"})
    return blob, eid


def run(sub, tmp_path, gen, **kw):
    program, cause = commit_bundle(sub, tmp_path, gen)
    args = {"island_best_sweep": None, "salts": [1], "max_workers": None, "full_max_x": 4096} | kw
    return program, verify.evaluate(sub, program, workdir=tmp_path / "work", cause=cause, **args)


def events(sub, program, t):
    return sub.history("prog:" + program, types=[t])


def test_quick_check_reference(tmp_path, sub):
    from loop.programs import build, unpack
    program, _ = commit_bundle(sub, tmp_path, REFERENCE)
    unpack(sub.get_blob(program), tmp_path / "w")
    r = build(tmp_path / "w", tmp_path / "w" / "c.kmb")
    q = verify.sweep_check(r.kmb)
    assert q.passes and q.metrics["Q"] == 260 and q.metrics["T"] == 7735 and q.shots == 3133452


def test_build_failure_is_rejected_with_traceback(tmp_path, sub):  # Review Focus 1
    program, out = run(sub, tmp_path, "raise RuntimeError('boom')\n")
    [rej] = events(sub, program, "candidate.rejected")
    assert out.kind == "rejected" and rej.payload["reason"] == "build_failed"
    assert "Traceback" in rej.payload["detail"] and rej.cause is not None
    [spent] = sub.history("pool:cpu", types=["budget.spent"])
    assert spent.cause == rej.cause and spent.seq < rej.seq


def test_build_timeout_is_rejected(tmp_path, sub, monkeypatch):
    monkeypatch.setattr(verify, "BUILD_TIMEOUT", 1)
    program, out = run(sub, tmp_path, "import time\ntime.sleep(30)\n")
    [rej] = events(sub, program, "candidate.rejected")
    assert rej.payload["reason"] == "build_failed" and "timed out" in rej.payload["detail"]


def test_wrong_circuit_fails_quick_check(tmp_path, sub):
    program, out = run(sub, tmp_path, WRONG)
    [rej] = events(sub, program, "candidate.rejected")
    assert rej.payload["reason"] == "quick_check_failed" and rej.payload["circuit"].startswith("sha256:")


def test_not_better_than_island_best(tmp_path, sub):
    program, out = run(sub, tmp_path, REFERENCE, island_best_sweep=1.0, audit_rate=0.0)
    [rej] = events(sub, program, "candidate.rejected")
    assert rej.payload["reason"] == "not_better" and out.cost > 1.0


def test_improvement_gets_full_check_and_measurement(tmp_path, sub):
    program, out = run(sub, tmp_path, REFERENCE, salts=[1, 2])
    [m] = events(sub, program, "candidate.measured")
    p = m.payload
    assert out.kind == "measured" and p["passes"] and p["seeds"] == [1, 2]
    assert p["verifier"] == config.verifier_tag() and p["cost_basis"] == "invsqrt/QxC/v1"
    assert p["metrics"]["Q"] == 260 and p["shots"] == 2 * 4096 * 64


def test_measurement_records_sweep_cost(tmp_path, sub):
    program, out = run(sub, tmp_path, REFERENCE)
    [m] = events(sub, program, "candidate.measured")
    assert m.payload["metrics"]["sweep_cost"] > 0


def test_identical_circuit_is_rejected_as_duplicate(tmp_path, sub):
    run(sub, tmp_path, REFERENCE)
    program, out = run(sub, tmp_path, REFERENCE + "\n# only a comment changed\n")
    [rej] = events(sub, program, "candidate.rejected")
    assert out.kind == "rejected" and rej.payload["reason"] == "duplicate"
    assert events(sub, program, "candidate.measured") == []


def test_full_check_failing_every_salt_is_recorded(tmp_path, sub, monkeypatch):  # review C2
    failing = verify.CheckResult(passes=False, cost=None, metrics={"seconds": 1.0}, shots=0, seeds=[1, 2],
                                 error="phase out of tolerance at x=3000001")
    monkeypatch.setattr(verify, "full_check", lambda *a, **k: failing)
    program, out = run(sub, tmp_path, REFERENCE, salts=[1, 2])
    [m] = events(sub, program, "candidate.measured")
    assert out.kind == "measured" and m.payload["passes"] is False and "shots" not in m.payload
    assert "x=3000001" in m.payload["failure"]["detail"]


def test_check_timeout_is_a_failure_not_a_hang(tmp_path, sub, monkeypatch):  # review I3
    monkeypatch.setattr(verify, "SWEEP_TIMEOUT", 0.01)
    program, out = run(sub, tmp_path, REFERENCE)
    [rej] = events(sub, program, "candidate.rejected")
    assert rej.payload["reason"] == "quick_check_failed" and "timed out" in rej.payload["detail"]
    assert rej.payload["tier"] == "sweep"


LARGE_X_WRONG = REFERENCE.replace(
    "        _phase_by_bits(builder, y1)\n",
    "        _phase_by_bits(builder, y1)\n        builder.z(x[width - 1])  # flips the phase only for x >= 2**21\n")


def test_circuit_wrong_only_at_large_x_is_stopped_by_the_sweep(tmp_path, sub):  # Review Focus 5
    assert LARGE_X_WRONG != REFERENCE
    program, out = run(sub, tmp_path, LARGE_X_WRONG)
    [rej] = events(sub, program, "candidate.rejected")
    assert rej.payload["reason"] == "quick_check_failed" and rej.payload["tier"] == "sweep"
    assert events(sub, program, "candidate.measured") == []


@pytest.fixture(scope="module")
def ref_sweep_cost(tmp_path_factory):
    from loop.programs import build
    d = tmp_path_factory.mktemp("ref")
    (d / "gen.py").write_text(REFERENCE)
    return verify.sweep_check(build(d, d / "c.kmb").kmb).cost


def test_gate_margin(tmp_path, sub, ref_sweep_cost):
    program, out = run(sub, tmp_path, REFERENCE, island_best_sweep=ref_sweep_cost / 1.03, audit_rate=0.0)
    assert out.kind == "measured"  # 3% worse: within the 5% margin, so fully checked


def test_gate_rejects_clearly_worse_with_estimate(tmp_path, sub, ref_sweep_cost):
    program, out = run(sub, tmp_path, REFERENCE, island_best_sweep=ref_sweep_cost / 1.10, audit_rate=0.0)
    [rej] = events(sub, program, "candidate.rejected")
    assert rej.payload["reason"] == "not_better" and rej.payload["tier"] == "gate"
    assert rej.payload["estimate"]["cost"] == pytest.approx(ref_sweep_cost) and rej.payload["estimate"]["Q"] == 260


def test_audit_fully_checks_a_would_be_rejection(tmp_path, sub, ref_sweep_cost):
    program, out = run(sub, tmp_path, REFERENCE, island_best_sweep=ref_sweep_cost / 1.10, audit_rate=1.0)
    [m] = events(sub, program, "candidate.measured")
    assert m.payload["metrics"]["audited"] is True


def test_full_check_stops_at_first_failing_salt(tmp_path, sub, monkeypatch):
    real = verify._run_verifier

    def fail_salt_1(kmb, extra, timeout):
        if "--salt" in extra and extra[extra.index("--salt") + 1] == "1":
            return verify.CheckResult(False, None, {"seconds": 0.1}, 0, [], "phase error at x=7")
        return real(kmb, extra, timeout)

    monkeypatch.setattr(verify, "_run_verifier", fail_salt_1)
    program, out = run(sub, tmp_path, REFERENCE, salts=[1, 2, 3])
    [m] = events(sub, program, "candidate.measured")
    assert m.payload["passes"] is False and m.payload["seeds"] == [1]
