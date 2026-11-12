import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from loop import policy
from substrate import connect
from substrate.cli import Forbidden, Substrate
from substrate.client import RemoteSubstrate
from substrate.server import make_server
from substrate.types import Candidate

P = "sha256:" + "1" * 64
C1, C2 = "sha256:" + "2" * 64, "sha256:" + "3" * 64


@pytest.fixture
def remote(tmp_path):
    sockdir = Path(tempfile.mkdtemp(dir="/tmp", prefix="sc-"))
    who = {"user": "rl-verifier"}
    srv = make_server(sockdir / "api.sock", tmp_path / "data", policy=policy.load(),
                      uid_to_user=lambda uid: who["user"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield RemoteSubstrate(sockdir / "api.sock", retry_s=2), who
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(sockdir, ignore_errors=True)


def measure(sub, circuit, cost, **kw):
    payload = {"program": P, "circuit": circuit, "cost": cost, "passes": True, "benchmark": "invsqrt",
               "verifier": "v", "cost_basis": "b"} | kw
    return sub.append("candidate.measured", payload, subject="prog:" + P, actor={"model": "x"})


def test_frontier_roundtrip_returns_dataclasses(remote):
    sub, _ = remote
    measure(sub, C1, 100.0)
    measure(sub, C2, 50.0)
    top = sub.frontier("invsqrt", verifier="v", cost_basis="b")
    assert [c.circuit for c in top] == [C2, C1] and isinstance(top[0], Candidate)
    events, cursor = sub.since(None)
    assert cursor == events[-1].seq and events[-1].actor["role"] == "verifier"


def test_forbidden_is_reraised(remote):
    sub, who = remote
    who["user"] = "rl-worker"
    with pytest.raises(Forbidden):
        measure(sub, C1, 1.0)


def test_blob_roundtrip(remote):
    sub, _ = remote
    assert sub.get_blob(sub.put_blob(b"abc")) == b"abc"


def test_append_retried_after_lost_reply_is_written_once(remote, monkeypatch):  # Review Focus 3
    sub, _ = remote
    real = sub._request
    calls = []

    def lose_first_reply(method, path, body, raw=False):
        out = real(method, path, body, raw)
        calls.append(path)
        if path == "/v1/append" and len(calls) == 1:
            raise ConnectionResetError("reply lost")  # server committed, client never heard back
        return out

    monkeypatch.setattr(sub, "_request", lose_first_reply)
    eid = measure(sub, C1, 10.0)
    events, _ = sub.since(None, types=["candidate.measured"])
    assert len(events) == 1 and events[0].id == eid and calls.count("/v1/append") == 2


def test_in_process_append_with_same_id_is_idempotent(tmp_path):
    s = Substrate(tmp_path)
    a = s.append("question.asked", {"question": "q"}, subject="bench:x", actor={}, id="01JC7KQF8R2M4VX1A0BZPQ3XYZ")
    b = s.append("question.asked", {"question": "q"}, subject="bench:x", actor={}, id="01JC7KQF8R2M4VX1A0BZPQ3XYZ")
    assert a == b and len(s.history("bench:x")) == 1


def test_connect_picks_transport(tmp_path):
    assert isinstance(connect(str(tmp_path / "d")), Substrate)
    assert isinstance(connect("unix:/tmp/nope.sock"), RemoteSubstrate)
