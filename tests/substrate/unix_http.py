"""Tiny HTTP-over-Unix-socket helper for tests."""

import http.client
import json
import socket


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def call(path, method, url, body=None, raw=False):
    conn = UnixHTTPConnection(str(path))
    data = body if raw else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, url, body=data, headers={"Content-Type": "application/octet-stream" if raw else "application/json"})
    resp = conn.getresponse()
    out = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(out)
    except ValueError:
        return resp.status, out
