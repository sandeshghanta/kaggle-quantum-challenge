"""Regression tests for the final-review findings."""

import json
import sqlite3
import subprocess
import sys

import pytest
from conftest import A, B, C, C1, C2, C3, commit, measure

from substrate import _projections
from substrate._projections import candidates, frontier, lineage
from substrate._schemas import SchemaViolation, UnknownVersion
from substrate._store import Busy, Store


def _cli(root, *args):
    return subprocess.run([sys.executable, "-m", "substrate.cli", "--root", str(root), *args],
                          capture_output=True, text=True)


# 1. NaN / Infinity must never reach the log
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nan_and_infinity_rejected(st, bad):
    with pytest.raises(SchemaViolation):
        measure(st, C1, bad)
    assert st.since(None) == ([], 0)


def test_cli_rejects_nan_literal(tmp_path):
    r = _cli(tmp_path, "append", "--type", "question.asked", "--subject", "bench:x",
             "--actor", "{}", "--payload", '{"question": NaN}')
    assert r.returncode == 2 and json.loads(r.stderr)["error"] == "ValueError"


# 2. lineage computes candidates once per call, not once per node
def test_lineage_reads_candidates_once(st, monkeypatch):
    commit(st, A, None)
    commit(st, B, A)
    commit(st, C, B)
    calls = []
    real = _projections.candidates
    monkeypatch.setattr(_projections, "candidates", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert [n.program for n in lineage(st, A, direction="down")] == [B, C]
    assert len(calls) == 1


# 3. cost bases are never merged inside one Candidate, and frontier can pin a basis
def test_candidate_does_not_mix_cost_bases(st):
    measure(st, C1, 5.0, basis="new")
    measure(st, C1, 1e13, basis="old")
    [c] = candidates(st, "invsqrt")
    assert c.cost_basis == "old" and c.measurements == 1 and c.cost == 1e13


def test_frontier_pinned_basis_ignores_stray_writer(st):
    measure(st, C1, 10.0, basis="new")
    measure(st, C2, 7.0, basis="old")  # a stale script writes later
    assert [c.circuit for c in frontier(st, "invsqrt", cost_basis="new")] == [C1]


# 4. a held write lock surfaces as Busy, and the CLI reports non-schema errors as JSON
def test_lock_timeout_raises_busy(tmp_path):
    path = tmp_path / "e.sqlite"
    st = Store(path, timeout=0.2)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Busy):
            st.append("question.asked", {"question": "q"}, subject="bench:x", actor={})
    finally:
        holder.execute("ROLLBACK")


def test_cli_reports_missing_file_as_json(tmp_path):
    r = _cli(tmp_path, "put-blob", str(tmp_path / "nope.bin"))
    assert r.returncode == 2 and json.loads(r.stderr)["error"] == "OSError"


# 5. an unknown event type on read is UnknownVersion, not SchemaViolation
def test_unknown_type_on_read_is_unknown_version(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    db = sqlite3.connect(tmp_path / "e.sqlite")
    db.execute("INSERT INTO events (v,id,ts,type,type_v,subject,actor,cause,payload) VALUES "
               "(1,'01JC7KQF8R2M4VX1A0BZPQ3XYZ','2026-11-11T00:00:00.000Z','foo.bar',1,'bench:x','{}',NULL,'{}')")
    db.commit()
    with pytest.raises(UnknownVersion):
        st.history("bench:x")
