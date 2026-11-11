import pytest

from substrate._store import Store


def _d(ch: str) -> str:
    return "sha256:" + ch * 64


P, A, B, C, D = _d("1"), _d("a"), _d("b"), _d("c"), _d("d")
C1, C2, C3, C4 = _d("2"), _d("3"), _d("4"), _d("5")


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path / "e.sqlite")


def spend(st, pool, ts, monkeypatch, **fields):
    from substrate import _ulid
    monkeypatch.setattr(_ulid, "utc_now_iso", lambda: ts)
    payload = {"pool": pool, "tokens_in": 0, "tokens_out": 0} | fields
    eid = st.append("budget.spent", payload, subject="pool:" + pool, actor={})
    monkeypatch.undo()
    return eid


def commit(st, blob, parent):
    payload = {"blob": blob, "parent": parent, "benchmark": "invsqrt"}
    return st.append("program.committed", payload, subject="prog:" + blob, actor={})


def measure(st, circuit, cost, passes=True, verifier="kickmix@1", basis="invsqrt/QxC/v1", program=P):
    payload = {"program": program, "circuit": circuit, "cost": cost, "passes": passes,
               "benchmark": "invsqrt", "verifier": verifier, "cost_basis": basis}
    return st.append("candidate.measured", payload, subject="prog:" + program, actor={})
