"""substrated over TCP: callers are identified by role tokens, not Unix users."""

import threading

import pytest

from loop import policy
from substrate import connect
from substrate.cli import Forbidden
from substrate.client import RemoteSubstrate
from substrate.server import load_tokens, make_server

TOKENS = {"worker": "w" * 32, "verifier": "v" * 32}
PROG = "prog:sha256:" + "a" * 64
ANN = {"island": "main", "plan": "p"}


@pytest.fixture
def url(tmp_path):
    srv = make_server(None, tmp_path / "data", policy=policy.load(), listen=("127.0.0.1", 0),
                      tokens={t: r for r, t in TOKENS.items()})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def client(url, token):
    return RemoteSubstrate(url=url, token=token, retry_s=0)


def test_role_comes_from_the_token_not_the_claimed_actor(url):
    w = client(url, TOKENS["worker"])
    w.append("attempt.announced", ANN, subject=PROG, actor={"role": "verifier", "model": "m"})
    [ev] = w.history(PROG)
    assert ev.actor == {"role": "worker", "model": "m"}


def test_policy_still_applies_per_role(url):
    with pytest.raises(Forbidden):
        client(url, TOKENS["worker"]).append("candidate.measured", {"program": "sha256:" + "a" * 64},
                                              subject=PROG, actor={})


@pytest.mark.parametrize("token", [None, "", "x" * 32, TOKENS["worker"] + "x"])
def test_missing_or_wrong_token_is_refused(url, token):
    with pytest.raises(Forbidden):
        client(url, token).since(None)


def test_blobs_roundtrip_with_a_token(url):
    w = client(url, TOKENS["worker"])
    d = w.put_blob(b"hello")
    assert w.get_blob(d) == b"hello"


def test_connect_reads_the_token_file(url, tmp_path, monkeypatch):
    f = tmp_path / "worker"
    f.write_text(TOKENS["worker"] + "\n")
    monkeypatch.setenv("SUBSTRATE_TOKEN_FILE", str(f))
    sub = connect(url)
    assert isinstance(sub, RemoteSubstrate) and sub.token == TOKENS["worker"]
    assert all(ev.type == "schema.registered" for ev in sub.since(None)[0])


def test_load_tokens_skips_missing_roles_and_refuses_short_ones(tmp_path):
    (tmp_path / "worker").write_text("w" * 32)
    assert load_tokens(tmp_path, ["worker", "verifier"]) == {"w" * 32: "worker"}
    (tmp_path / "verifier").write_text("short")
    with pytest.raises(ValueError, match="verifier"):
        load_tokens(tmp_path, ["worker", "verifier"])


def test_tcp_with_policy_needs_tokens(tmp_path):
    with pytest.raises(ValueError, match="tokens"):
        make_server(None, tmp_path / "d", policy=policy.load(), listen=("127.0.0.1", 0))


def test_client_needs_exactly_one_transport():
    with pytest.raises(ValueError):
        RemoteSubstrate()
    with pytest.raises(ValueError):
        RemoteSubstrate("/tmp/x.sock", url="http://h:1")
