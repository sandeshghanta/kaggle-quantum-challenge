"""Run one pi job headlessly (JSON mode) against an OpenAI-compatible endpoint."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PROVIDER = "qwen"


def write_agent_dir(dest: Path, *, base_url: str, model: str, api_key_env: str,
                    context_window: int, max_tokens: int) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    models = {"providers": {PROVIDER: {
        "baseUrl": base_url, "api": "openai-completions", "apiKey": f"${api_key_env}",
        "models": [{"id": model, "contextWindow": context_window, "maxTokens": max_tokens,
                    "compat": {"thinkingFormat": "qwen"}}],
    }}}
    (dest / "models.json").write_text(json.dumps(models, indent=2))
    return dest


@dataclass(frozen=True)
class PiRun:
    ok: bool
    exit_code: int | None
    final_text: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    seconds: float
    error: str | None


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def run_pi(workdir: Path, agent_dir: Path, *, prompt: str, system_append: Path, tools: list[str],
           model: str, timeout_s: float) -> PiRun:
    cmd = ["pi", "--mode", "json", "--no-session", "--no-approve", "-nc",
           "--provider", PROVIDER, "--model", f"{PROVIDER}/{model}",
           *(["--tools", ",".join(tools)] if tools else ["-nt"]),
           "--append-system-prompt", str(system_append), prompt]
    env = os.environ | {"PI_CODING_AGENT_DIR": str(agent_dir)}
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    final, usage, buf, err = "", {}, b"", b""
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    sel.register(proc.stderr, selectors.EVENT_READ)
    timed_out = False
    while sel.get_map():
        remaining = timeout_s - (time.monotonic() - start)
        if remaining <= 0:
            timed_out = True
            break
        for key, _ in sel.select(timeout=min(remaining, 1.0)):
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                sel.unregister(key.fileobj)
                continue
            if key.fileobj is proc.stderr:
                err = (err + chunk)[-8000:]
                continue
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    ev = json.loads(line.rstrip(b"\r"))
                except ValueError:
                    continue
                if ev.get("usage"):
                    usage = ev["usage"]
                if ev.get("type") == "message_end" and ev.get("message", {}).get("role") == "assistant":
                    final = _text(ev["message"])
                    usage = ev["message"].get("usage") or usage
    if timed_out:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    else:
        proc.wait()
    seconds = time.monotonic() - start
    tokens = dict(tokens_in=int(usage.get("input", 0)) + int(usage.get("cacheRead", 0)),
                  tokens_out=int(usage.get("output", 0)), tokens_cached=int(usage.get("cacheRead", 0)))
    if timed_out:
        return PiRun(False, None, final, seconds=seconds, error="timeout", **tokens)
    ok = proc.returncode == 0
    return PiRun(ok, proc.returncode, final, seconds=seconds,
                 error=None if ok else err.decode(errors="replace")[-2000:] or f"exit {proc.returncode}", **tokens)
