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
    args = {"island_best": None, "salts": [1], "max_workers": None, "full_max_x": 4096} | kw
    return program, verify.evaluate(sub, program, workdir=tmp_path / "work", cause=cause, **args)


def events(sub, program, t):
    return sub.history("prog:" + program, types=[t])


def test_quick_check_reference(tmp_path, sub):
    from loop.programs import build, unpack
    program, _ = commit_bundle(sub, tmp_path, REFERENCE)
    unpack(sub.get_blob(program), tmp_path / "w")
    r = build(tmp_path / "w", tmp_path / "w" / "c.kmb")
    q = verify.quick_check(r.kmb)
    assert q.passes and q.metrics["Q"] == 260 and q.metrics["T"] == 7735 and q.shots == 65536 * 64


def test_build_failure_is_rejected_with_traceback(tmp_path, sub):  # Review Focus 1
    program, out = run(sub, tmp_path, "raise RuntimeError('boom')\n")
    [rej] = events(sub, program, "candidate.rejected")
    assert out.kind == "rejected" and rej.payload["reason"] == "build_failed"
    assert "Traceback" in rej.payload["detail"] and rej.cause is not None
    [spent] = sub.history("pool:cpu", types=["budget.spent"])
    assert spent.cause == out.event_id


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
    program, out = run(sub, tmp_path, REFERENCE, island_best=1.0)
    [rej] = events(sub, program, "candidate.rejected")
    assert rej.payload["reason"] == "not_better" and out.cost > 1.0


def test_improvement_gets_full_check_and_measurement(tmp_path, sub):
    program, out = run(sub, tmp_path, REFERENCE, salts=[1, 2])
    [m] = events(sub, program, "candidate.measured")
    p = m.payload
    assert out.kind == "measured" and p["passes"] and p["seeds"] == [1, 2]
    assert p["verifier"] == config.verifier_tag() and p["cost_basis"] == "invsqrt/QxC/v1"
    assert p["metrics"]["Q"] == 260 and p["shots"] == 2 * 4096 * 64
