"""Local status page server: serves the dashboard and two JSON files, GET only, nothing else.

Exactly four URLs exist: /, /live.json, /researcher.json. Paths are matched, never joined onto a
directory, so there is nothing to traverse and no directory listing.
"""

from __future__ import annotations

import argparse
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from loop import config

DEFAULT_INDEX = config.REPO / "dashboard" / "index.html"


def make_server(host: str, port: int, *, index: Path, live: Path, researcher: Path) -> ThreadingHTTPServer:
    routes = {"/": (Path(index), "text/html; charset=utf-8"),
              "/live.json": (Path(live), "application/json"),
              "/researcher.json": (Path(researcher), "application/json")}

    class Handler(BaseHTTPRequestHandler):
        server_version = "statuspage"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _send(self, status: int, body: bytes, ctype: str, head_only: bool = False) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _serve(self, head_only: bool) -> None:
            route = routes.get(urlsplit(self.path).path)
            if route is None:
                return self._send(404, b'{"error": "not found"}', "application/json", head_only)
            path, ctype = route
            try:
                body = path.read_bytes()
            except OSError:
                return self._send(404, b"{}", "application/json", head_only)
            self._send(200, body, ctype, head_only)

        def do_GET(self):
            self._serve(False)

        def do_HEAD(self):
            self._serve(True)

        def _refuse(self):
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    return ThreadingHTTPServer((host, port), Handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--index", default=str(DEFAULT_INDEX))
    p.add_argument("--live", required=False, default="live.json", help="live.json written by loop.live")
    p.add_argument("--researcher", default="researcher.json", help="researcher.json written by the researcher")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    srv = make_server(args.host, args.port, index=Path(args.index), live=Path(args.live),
                      researcher=Path(args.researcher))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=srv.shutdown, daemon=True).start())
    print(f"statuspage: http://{args.host}:{srv.server_address[1]}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
