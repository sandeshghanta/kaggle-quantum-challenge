import http.client
import json
import socket
import threading

import pytest

from loop import statuspage


@pytest.fixture
def server(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<title>hi</title>")
    (tmp_path / "secret.txt").write_text("secret")
    live = tmp_path / "pub" / "live.json"
    live.parent.mkdir()
    live.write_text('{"a": 1}')
    srv = statuspage.make_server("127.0.0.1", 0, index=page, live=live, researcher=tmp_path / "pub" / "researcher.json")
    t = threading.Thread(target=lambda: srv.serve_forever(0.05), daemon=True)
    t.start()
    yield srv, tmp_path
    srv.shutdown()
    srv.server_close()


def get(srv, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request(method, path)
    r = c.getresponse()
    return r.status, r.getheader("Content-Type"), r.getheader("Cache-Control"), r.read()


def test_index(server):
    s, ct, cc, body = get(server[0], "/")
    assert s == 200 and ct.startswith("text/html") and body == b"<title>hi</title>" and cc == "no-store"


def test_live_json(server):
    s, ct, cc, body = get(server[0], "/live.json")
    assert s == 200 and ct.startswith("application/json") and json.loads(body) == {"a": 1} and cc == "no-store"


def test_missing_researcher_is_404_json_empty_object(server):
    s, ct, _, body = get(server[0], "/researcher.json")
    assert s == 404 and ct.startswith("application/json") and json.loads(body) == {}


def test_researcher_served_when_present(server):
    srv, tmp = server
    (tmp / "pub" / "researcher.json").write_text('{"r": 2}')
    assert json.loads(get(srv, "/researcher.json")[3]) == {"r": 2}


def test_query_string_ignored(server):
    assert get(server[0], "/live.json?x=1")[0] == 200


@pytest.mark.parametrize("path", ["/other", "/index.html", "/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt",
                                  "/live.json/../../secret.txt", "//etc/passwd", "/pub/", "/pub", "/live.json/"])
def test_everything_else_is_404(server, path):
    s, ct, _, body = get(server[0], path)
    assert s == 404 and b"secret" not in body


def test_raw_dotdot_request_cannot_escape(server):
    srv, _ = server
    sock = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    sock.sendall(b"GET /../../../../etc/passwd HTTP/1.0\r\n\r\n")
    data = sock.recv(65536)
    sock.close()
    assert data.startswith(b"HTTP/1.") and b" 404 " in data.split(b"\r\n")[0] and b"root:" not in data


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_get_only(server, method):
    assert get(server[0], "/live.json", method)[0] in (405, 501)


def test_head_allowed_like_get(server):
    s, _, _, body = get(server[0], "/live.json", "HEAD")
    assert s == 200 and body == b""


def test_default_host_is_loopback():
    assert statuspage.parse_args([]).host == "127.0.0.1" and statuspage.parse_args([]).port == 8080
