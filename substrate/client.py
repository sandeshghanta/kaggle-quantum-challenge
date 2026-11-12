"""RemoteSubstrate: the Substrate API over substrated (Unix socket, or HTTP with a role token).

Same methods and return types as the in-process Substrate. Connection failures and 503 (Busy) are
retried with backoff for up to `retry_s` seconds, so callers survive a substrated restart. Appends
are retry-safe: the client picks the event id, and the server treats a repeated id as already done.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from substrate import _ulid
from substrate._blobs import BlobNotFound
from substrate._schemas import SchemaViolation, UnknownVersion
from substrate._store import Busy
from substrate.cli import Forbidden
from substrate.types import BudgetState, Candidate, Event, Node

_ERRORS = {"SchemaViolation": SchemaViolation, "Forbidden": Forbidden, "BlobNotFound": BlobNotFound,
           "UnknownVersion": UnknownVersion, "Busy": Busy, "ValueError": ValueError}
_RETRYABLE = (ConnectionError, FileNotFoundError, socket.timeout, Busy, http.client.HTTPException)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class RemoteSubstrate:
    def __init__(self, socket_path: Path | str | None = None, *, url: str | None = None, token: str | None = None,
                 retry_s: float = 60.0, timeout: float = 300.0):
        """Either `socket_path` (Unix socket) or `url` ("http://host:port", with the role `token`)."""
        if (socket_path is None) == (url is None):
            raise ValueError("give exactly one of socket_path and url")
        self.socket_path = str(socket_path) if socket_path is not None else None
        self.url = urlsplit(url) if url else None
        self.token = token
        self.retry_s = retry_s
        self.timeout = timeout

    # ── transport ──────────────────────────────────────────────────────
    def _connection(self) -> http.client.HTTPConnection:
        if self.url is not None:
            return http.client.HTTPConnection(self.url.hostname, self.url.port or 80, timeout=self.timeout)
        return _UnixHTTPConnection(self.socket_path, self.timeout)

    def _request(self, method: str, path: str, body, raw: bool = False):
        conn = self._connection()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            data = body if raw or body is None else json.dumps(body, allow_nan=False).encode()
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        finally:
            conn.close()
        if resp.status == 200:
            ctype = resp.getheader("Content-Type", "")
            return payload if ctype.startswith("application/octet-stream") else json.loads(payload)
        try:
            err = json.loads(payload)
        except ValueError:
            err = {"error": "HTTPError", "message": payload.decode(errors="replace")}
        raise _ERRORS.get(err.get("error"), RuntimeError)(err.get("message", f"HTTP {resp.status}"))

    def _call(self, method: str, path: str, body=None, raw: bool = False):
        deadline, delay = time.monotonic() + self.retry_s, 0.2
        while True:
            try:
                return self._request(method, path, body, raw)
            except _RETRYABLE:
                if time.monotonic() + delay > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    # ── write ──────────────────────────────────────────────────────────
    def put_blob(self, data: bytes) -> str:
        return self._call("POST", "/v1/put_blob", data, raw=True)["digest"]

    def append(self, type: str, payload: dict, *, subject: str, actor: dict,
               cause: str | None = None, type_v: int | None = None) -> str:
        body = {"id": _ulid.new_ulid(), "type": type, "payload": payload, "subject": subject,
                "actor": actor, "cause": cause, "type_v": type_v}
        return self._call("POST", "/v1/append", body)["id"]

    # ── read ───────────────────────────────────────────────────────────
    def get_blob(self, digest: str) -> bytes:
        return self._call("GET", f"/v1/blob/{digest}")

    def since(self, cursor: int | None, *, types: list[str] | None = None,
              limit: int = 1000) -> tuple[list[Event], int]:
        out = self._call("POST", "/v1/since", {"cursor": cursor, "types": types, "limit": limit})
        return [Event(**e) for e in out["events"]], out["cursor"]

    def history(self, subject: str, *, types: list[str] | None = None) -> list[Event]:
        return [Event(**e) for e in self._call("POST", "/v1/history", {"subject": subject, "types": types})]

    def frontier(self, benchmark: str, *, k: int = 20, verifier: str | None = None,
                 cost_basis: str | None = None) -> list[Candidate]:
        out = self._call("POST", "/v1/frontier", {"benchmark": benchmark, "k": k, "verifier": verifier,
                                                  "cost_basis": cost_basis})
        return [Candidate(**c) for c in out]

    def lineage(self, ref: str, *, direction: str = "up") -> list[Node]:
        return [Node(**n) for n in self._call("POST", "/v1/lineage", {"ref": ref, "direction": direction})]

    def budget(self, pool: str, *, window: str = "7d") -> BudgetState:
        return BudgetState(**self._call("POST", "/v1/budget", {"pool": pool, "window": window}))
