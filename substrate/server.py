"""substrated: the substrate as a service on a Unix socket, with OS-attested callers.

The server is the only process that can open the event log and blobs. Each request's caller is
identified by its Unix uid (SO_PEERCRED on Linux, LOCAL_PEERCRED on macOS), mapped to a user name,
then to a role via the policy. The server stamps `actor.role` itself, so a caller cannot claim to
be someone else. The policy decides what each role may write; everyone may read.

Protocol: JSON over HTTP/1.1.
  POST /v1/<method>   body = JSON object of keyword arguments (append, since, history, frontier,
                      lineage, budget)
  POST /v1/put_blob   body = raw bytes -> {"digest": ...}
  GET  /v1/blob/<d>   -> raw bytes
Errors: {"error": <class>, "message": ...} with 400/403/404/409/413/503.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pwd
import socket
import socketserver
import struct
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from substrate._blobs import BlobNotFound
from substrate._schemas import SchemaViolation, UnknownVersion
from substrate._store import Busy
from substrate.cli import Forbidden, Substrate

READS = ("since", "history", "frontier", "lineage", "budget")
STATUS = [(Forbidden, 403), (BlobNotFound, 404), (UnknownVersion, 409), (Busy, 503),
          (SchemaViolation, 400), (ValueError, 400)]


def peer_uid(sock: socket.socket) -> int | None:
    if hasattr(socket, "SO_PEERCRED"):  # Linux: struct ucred {pid, uid, gid}
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid
    if sys.platform == "darwin":  # SOL_LOCAL=0, LOCAL_PEERCRED=1: struct xucred {version, uid, ...}
        _, uid = struct.unpack_from("II", sock.getsockopt(0, 1, 76))
        return uid
    return None


def pwd_lookup(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _jsonable(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    return obj


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(socket_path: Path, root: Path, *, policy=None, uid_to_user=pwd_lookup,
                max_body: int = 64 << 20) -> _Server:
    sub = Substrate(root, authorize=policy.authorize if policy else None)
    sub.init()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def address_string(self):
            return "unix"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj, allow_nan=False).encode())

        def _error(self, e: BaseException) -> None:
            status = next((code for cls, code in STATUS if isinstance(e, cls)), 500)
            self._json(status, {"error": type(e).__name__, "message": str(e)})

        def _principal(self) -> tuple[bool, str | None]:
            """(allowed, role). Without a policy every caller is unrestricted (principal None)."""
            if policy is None:
                return True, None
            uid = peer_uid(self.connection)
            user = uid_to_user(uid) if uid is not None else None
            role = policy.role_for_user(user) if user else None
            return role is not None, role

        def _body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:  # a negative length would make read() unbounded
                self.close_connection = True
                self._json(400, {"error": "ValueError", "message": "invalid Content-Length"})
                return None
            if length > max_body:
                remaining = length  # drain without buffering, so the client can read our answer
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self.close_connection = True
                self._json(413, {"error": "PayloadTooLarge", "message": f"body over {max_body} bytes"})
                return None
            return self.rfile.read(length)

        def do_GET(self):
            ok, _ = self._principal()
            if not ok:
                return self._json(403, {"error": "Forbidden", "message": "unknown caller"})
            if not self.path.startswith("/v1/blob/"):
                return self._json(404, {"error": "NotFound", "message": self.path})
            try:
                self._send(200, sub.get_blob(self.path[len("/v1/blob/"):]), "application/octet-stream")
            except Exception as e:
                self._error(e)

        def do_POST(self):
            ok, role = self._principal()  # before reading anything: unknown callers get nothing
            if not ok:
                self.close_connection = True
                return self._json(403, {"error": "Forbidden", "message": "unknown caller"})
            body = self._body()
            if body is None:
                return
            method = self.path.removeprefix("/v1/")
            try:
                if method == "put_blob":
                    return self._json(200, {"digest": sub.put_blob(body, principal=role)})
                args = json.loads(body or b"{}", parse_constant=_reject_constant)
                if not isinstance(args, dict):
                    raise ValueError("request body must be a JSON object")
                if method == "append":
                    actor = dict(args.get("actor") or {})
                    if role is not None:
                        actor["role"] = role  # attested, not claimed
                    eid = sub.append(args["type"], args["payload"], subject=args["subject"], actor=actor,
                                     cause=args.get("cause"), type_v=args.get("type_v"), principal=role,
                                     id=args.get("id"))
                    return self._json(200, {"id": eid})
                if method in READS:
                    result = getattr(sub, method)(**args)
                    if method == "since":
                        events, cursor = result
                        return self._json(200, {"events": _jsonable(events), "cursor": cursor})
                    return self._json(200, _jsonable(result))
                self._json(404, {"error": "NotFound", "message": method})
            except KeyError as e:
                self._error(ValueError(f"missing argument {e}"))
            except Exception as e:
                self._error(e)

    socket_path = Path(socket_path)
    socket_path.unlink(missing_ok=True)
    srv = _Server(str(socket_path), Handler)
    os.chmod(socket_path, 0o660)
    return srv


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--socket", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--policy", help="role policy JSON (loop/policy.json); omit for an unrestricted dev server")
    args = p.parse_args(argv)
    pol = None
    if args.policy:
        from loop import policy as policy_mod
        pol = policy_mod.load(Path(args.policy))
    srv = make_server(Path(args.socket), Path(args.root), policy=pol)
    print(f"substrated: serving {args.root} on {args.socket}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
