import shutil

import pytest

from loop import check, config
from tests.loop.test_verify import REFERENCE, WRONG

MARSYAS = config.REPO / "seeds" / "marsyas_invsqrt"


def bundle(tmp_path, gen):
    d = tmp_path / "b"
    d.mkdir()
    (d / "gen.py").write_text(gen)
    return d


def test_sample_includes_edges_and_is_deterministic():
    xs, n_edge = check.sample_xs(3133452, n_random=100)
    edge = set(xs[:n_edge].tolist())
    assert {1, 2, 3133452, 2**21, 2**21 - 1, 2**21 + 1} <= edge
    assert len(xs) == n_edge + 100 and (xs >= 1).all() and (xs <= 3133452).all()
    assert (check.sample_xs(3133452, n_random=100)[0] == xs).all()


def test_reference_passes_with_a_cost_estimate(tmp_path):
    r = check.check_bundle(bundle(tmp_path, REFERENCE), n_random=256)
    assert r.passes and r.stage == "ok" and r.error is None, r.error
    assert r.qubits and r.cost_estimate and 1.5e6 < r.cost_estimate < 2.6e6  # reference cost ~2.08M
    assert "PASS" in r.text() and "estimated cost" in r.text()


def test_build_failure_reports_the_traceback(tmp_path):
    r = check.check_bundle(bundle(tmp_path, "raise RuntimeError('boom in gen')\n"))
    assert not r.passes and r.stage == "build" and "boom in gen" in r.error
    assert r.text().startswith("BUILD FAILED")


def test_wrong_circuit_fails_with_the_first_bad_x(tmp_path):
    r = check.check_bundle(bundle(tmp_path, WRONG), n_random=64)
    assert not r.passes and r.stage == "check" and r.error.startswith("x=2: phase error")  # x=1 is a whole turn: trivially right


def test_marsyas_seed_passes_and_estimates_near_its_measured_cost(tmp_path):
    d = tmp_path / "m"
    shutil.copytree(MARSYAS, d)
    r = check.check_bundle(d, n_random=512)
    assert r.passes, r.error
    assert r.qubits == 92 and abs(r.cost_estimate - 148979) / 148979 < 0.05


def test_cli_exit_codes(tmp_path, capsys):
    assert check.main([str(bundle(tmp_path, "raise SystemExit(3)\n"))]) == 1
    assert "BUILD FAILED" in capsys.readouterr().out
    assert check.main([]) == 2
