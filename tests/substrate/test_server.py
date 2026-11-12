import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest
from unix_http import call

from loop import policy
from substrate.server import make_server

Q = {"question": "q"}


@pytest.fixture
def server(tmp_path):
    sockdir = Path(tempfile.mkdtemp(dir="/tmp", prefix="sd-"))
    who = {"user": "rl-worker"}
    srv = make_server(sockdir / "api.sock", tmp_path / "data", policy=policy.load(),
                      uid_to_user=lambda uid: who["user"], user_groups=lambda u: who.get("groups", []),
                      max_body=1 << 20)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield sockdir / "api.sock", who
    srv.shutdown()
    srv.server_close()
    shutil.rmtree(sockdir, ignore_errors=True)


def append(sock, type, payload, subject="bench:invsqrt", actor=None):
    return call(sock, "POST", "/v1/append", {"type": type, "payload": payload, "subject": subject,
                                             "actor": actor or {"model": "m"}})


ANN = {"island": "main", "plan": "pending"}


def test_append_roundtrip_and_role_is_stamped(server):  # Review Focus 1
    sock, who = server
    status, body = append(sock, "attempt.announced", ANN, subject="prog:sha256:" + "a" * 64,
                          actor={"role": "verifier", "model": "m"})
    assert status == 200 and len(body["id"]) == 26
    status, hist = call(sock, "POST", "/v1/history", {"subject": "prog:sha256:" + "a" * 64})
    assert status == 200 and hist[0]["actor"] == {"role": "worker", "model": "m"}


def test_worker_cannot_write_measurements(server):
    sock, _ = server
    status, body = append(sock, "candidate.measured", {"program": "sha256:" + "a" * 64})
    assert status == 403 and body["error"] == "Forbidden"


def test_unknown_user_is_refused(server):
    sock, who = server
    who["user"] = "mallory"
    status, body = call(sock, "POST", "/v1/since", {"cursor": None})
    assert status == 403


def test_oversized_body_is_refused_and_server_survives(server):  # Review Focus 4
    sock, _ = server
    status, _ = call(sock, "POST", "/v1/put_blob", b"x" * (2 << 20), raw=True)
    assert status == 413
    status, body = call(sock, "POST", "/v1/put_blob", b"small", raw=True)
    assert status == 200 and body["digest"].startswith("sha256:")


def test_blob_roundtrip_and_missing(server):
    sock, _ = server
    _, body = call(sock, "POST", "/v1/put_blob", b"abc", raw=True)
    status, data = call(sock, "GET", "/v1/blob/" + body["digest"])
    assert status == 200 and data == b"abc"
    status, err = call(sock, "GET", "/v1/blob/sha256:" + "0" * 64)
    assert status == 404 and err["error"] == "BlobNotFound"


def test_schema_violation_is_400(server):
    sock, _ = server
    status, body = append(sock, "attempt.announced", {"island": "main"})
    assert status == 400 and body["error"] == "SchemaViolation"


def test_socket_is_group_only(server):
    sock, _ = server
    assert oct(os.stat(sock).st_mode & 0o777) == "0o660"


def _raw(sock, request: bytes) -> bytes:
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(sock))
    s.sendall(request)
    data = s.recv(65536)
    s.close()
    return data


@pytest.mark.parametrize("length", ["-1", "abc"])
def test_bad_content_length_is_rejected_and_server_survives(server, length):  # security review I1
    sock, _ = server
    resp = _raw(sock, f"POST /v1/put_blob HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\nxxxx".encode())
    assert resp.startswith(b"HTTP/1.1 400")
    status, body = call(sock, "POST", "/v1/put_blob", b"small", raw=True)
    assert status == 200


def test_unknown_caller_is_refused_before_body_is_read(server):
    sock, who = server
    who["user"] = "mallory"
    resp = _raw(sock, b"POST /v1/put_blob HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 403")


def test_group_member_can_read_but_not_write(server):
    sock, who = server
    who["user"], who["groups"] = "alice", ["substrate-viewers"]
    assert call(sock, "POST", "/v1/since", {"cursor": None})[0] == 200
    status, body = append(sock, "attempt.announced", ANN, subject="prog:sha256:" + "a" * 64)
    assert status == 403 and body["error"] == "Forbidden"
