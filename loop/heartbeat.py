"""Worker heartbeats: one small JSON file per worker, rewritten atomically, read by the live updater.

Best effort by design: a status problem must never fail a job, so write errors are swallowed.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

_EDIT_TOOLS = {"edit", "write"}
_MIN_WRITE_GAP_S = 0.5


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_heartbeat(status_dir: Path | None, worker_id: int, **fields) -> None:
    if status_dir is None:
        return
    status_dir = Path(status_dir)
    data = {"worker": worker_id, **fields, "updated_at": _now()}
    try:
        status_dir.mkdir(parents=True, exist_ok=True)
        tmp = status_dir / f".worker-{worker_id}.json.tmp"
        tmp.write_text(json.dumps(data))
        os.replace(tmp, status_dir / f"worker-{worker_id}.json")
    except OSError:
        pass


class JobProgress:
    """Tracks one job's progress from pi JSON events and mirrors it into the heartbeat file."""

    def __init__(self, status_dir: Path | None, worker_id: int, *, island: str, parent: str, announcement: str):
        self.status_dir, self.worker_id = status_dir, worker_id
        self.base = {"island": island, "parent": parent, "announcement": announcement, "started_at": _now()}
        self.turns, self.tokens_out, self.last_tool, self.files = 0, 0, None, []
        self._last_write = 0.0
        self._write("thinking")

    def _write(self, state: str, **extra) -> None:
        self._last_write = time.monotonic()
        write_heartbeat(self.status_dir, self.worker_id, state=state, **self.base, turns=self.turns,
                        last_tool=self.last_tool, files_edited=list(self.files), tokens_out=self.tokens_out, **extra)

    def _tool(self, name, args) -> None:
        if not name:
            return
        self.last_tool = name
        path = args.get("path") if isinstance(args, dict) else None
        if name in _EDIT_TOOLS and path and path not in self.files:
            self.files.append(path)

    def on_event(self, ev: dict) -> None:
        kind = ev.get("type") or ""
        changed = False
        if kind.startswith("tool_execution_start"):
            self._tool(ev.get("toolName") or ev.get("tool"), ev.get("args"))
            changed = True
        elif kind == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
            msg = ev["message"]
            self.turns += 1
            self.tokens_out += int((msg.get("usage") or {}).get("output", 0) or 0)
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") in ("toolCall", "tool_use"):
                    self._tool(block.get("name"), block.get("arguments") or block.get("input"))
            changed = True
        if changed:
            self._write("thinking")

    def finish(self, outcome: str, summary: str) -> None:
        self._write("idle", last_outcome=outcome, last_summary=summary, finished_at=_now())
