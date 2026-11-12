"""A scripted OpenAI-compatible chat server for tests (stdlib only)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


class FakeOpenAI:
    def __init__(self, script: list[dict] | None = None, *, delay: float = 0.0, auto_edit: str | None = None,
                 port: int = 0):
        self.script = list(script or [])
        self.auto_edit = auto_edit
        self.port = port
        self.delay = delay
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def _next(self) -> dict:
        with self._lock:
            return self.script.pop(0) if self.script else {"text": "SUMMARY: nothing to do"}

    def _auto(self, req: dict) -> dict:
        """Stateless: first turn of a job edits gen.py, the turn after a tool result summarises."""
        last = (req.get("messages") or [{}])[-1]
        if last.get("role") == "tool":
            return {"text": "SUMMARY: fake edit for smoke testing"}
        return {"tool_calls": [{"name": "edit", "arguments": {"path": "gen.py", "edits": [
            {"oldText": self.auto_edit, "newText": self.auto_edit + "  # fake tweak"}]}}]}

    def __enter__(self) -> "FakeOpenAI":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"data": [{"id": "fake-model", "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fake.requests.append(req)
                if fake.delay:
                    time.sleep(fake.delay)
                item = fake._auto(req) if fake.auto_edit else fake._next()
                if item.get("tool_calls"):
                    calls = [{"index": i, "id": f"call_{i}", "type": "function",
                              "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                             for i, c in enumerate(item["tool_calls"])]
                    delta, finish = {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
                else:
                    delta, finish = {"role": "assistant", "content": item["text"]}, "stop"
                if req.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                                  {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                                  {"choices": [], "usage": USAGE}):
                        chunk |= {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": req.get("model")}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    msg = {k: v for k, v in delta.items() if v is not None}
                    body = json.dumps({"id": "chatcmpl-fake", "object": "chat.completion", "model": req.get("model"),
                                       "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                       "usage": USAGE}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Fake OpenAI server for smoke tests")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--auto-edit", default="import argparse", help="text in gen.py to tweak on each job")
    args = p.parse_args()
    with FakeOpenAI(auto_edit=args.auto_edit, port=args.port) as fake:
        print(f"fake model on {fake.base_url}", flush=True)
        threading.Event().wait()


if __name__ == "__main__":
    main()
