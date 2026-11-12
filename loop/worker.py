"""Lab assistant worker: pick a parent, announce, run one pi job, commit the result. Repeat."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from loop import config, pool
from loop.heartbeat import JobProgress, write_heartbeat
from loop.pi_runner import run_pi, write_agent_dir
from loop.programs import pack, unpack
from substrate import connect
from substrate.cli import Substrate

DEFAULT_ROLE_DIR = config.REPO / "roles" / "lab_assistant" / "v1"


@dataclass
class WorkerConfig:
    base_url: str
    model: str
    api_key_env: str
    context_window: int
    max_tokens: int
    role_dir: Path
    worker_id: int
    status_dir: Path | None = None


def _lessons(sub: Substrate) -> str:
    latest = sub.history("bench:" + config.BENCHMARK, types=["lessons.published"])
    if not latest:
        return "(none yet)"
    p = latest[-1].payload
    lines = [f"- works: {x}" for x in p.get("working", [])] + [f"- avoid: {x}" for x in p.get("not_working", [])]
    return "\n".join(lines) or "(none yet)"


def _note(final_text: str) -> str:
    for line in final_text.splitlines():
        if line.strip().startswith("SUMMARY:"):
            return line.split("SUMMARY:", 1)[1].strip()[:500]
    return final_text.strip()[:200] or "(no summary)"


def backoff_seconds(consecutive_failures: int) -> float:
    """Pause after repeated fast failures (endpoint down, bad key) so workers don't loop hot."""
    if consecutive_failures <= 0:
        return 0
    return min(300.0, 5.0 * 2 ** (consecutive_failures - 1))


def run_job(sub: Substrate, *, rng: random.Random, cfg: WorkerConfig, workroot: Path) -> str:
    role = json.loads((cfg.role_dir / "role.json").read_text())
    island = pool.pick_island(sub, rng)
    parent = pool.pick_parent(sub, island, rng)
    actor = config.actor("lab_assistant", role["version"], cfg.model)
    plan = f"{island}: improving parent {parent.removeprefix('sha256:')[:12]}"
    ann = sub.append("attempt.announced", {"island": island, "plan": plan},
                     subject="prog:" + parent, actor=actor, cause=pool.island_config_event(sub, island))
    job = Path(workroot) / ann
    progress = JobProgress(cfg.status_dir, cfg.worker_id, island=island, parent=parent, announcement=ann)
    outcome, summary = "failed", "crashed"
    try:
        bundle = job / "bundle"
        unpack(sub.get_blob(parent), bundle)
        before = pack(bundle)
        best = pool.island_best(sub, island)
        prompt = (cfg.role_dir / "task.md").read_text().format(
            parent_cost=f"{best:.6g}" if best else "not yet measured",
            island_instructions=pool.islands(sub)[island]["instructions"], lessons=_lessons(sub))
        agent = write_agent_dir(job / "agent", base_url=cfg.base_url, model=cfg.model, api_key_env=cfg.api_key_env,
                                context_window=cfg.context_window, max_tokens=cfg.max_tokens)
        run = run_pi(bundle, agent, prompt=prompt, system_append=cfg.role_dir / "system.md", tools=role["tools"],
                     model=cfg.model, timeout_s=role["timeout_s"], on_event=progress.on_event)
        sub.append("budget.spent", {"pool": "qwen", "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
                                    "tokens_cached": run.tokens_cached, "wall_seconds": round(run.seconds, 2)},
                   subject="pool:qwen", actor=actor, cause=ann)
        if not run.ok:
            outcome, summary = "failed", (run.error or "failed")[:200]
            return "failed"
        after = pack(bundle)
        if after == before:
            outcome, summary = "unchanged", _note(run.final_text)
            return "unchanged"
        program = sub.put_blob(after)
        # island.joined first: a crash between the two leaves an unjudged-but-placed program, never an orphan
        sub.append("island.joined", {"program": program, "island": island, "via": "submit"},
                   subject="prog:" + program, actor=actor, cause=ann)
        sub.append("program.committed", {"blob": program, "parent": parent, "benchmark": config.BENCHMARK,
                                         "note": _note(run.final_text)},
                   subject="prog:" + program, actor=actor, cause=ann)
        outcome, summary = "committed", _note(run.final_text)
        return "committed"
    finally:
        progress.finish(outcome, summary)
        shutil.rmtree(job, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--substrate", "--root", dest="substrate", required=True,
                   help="data directory (in-process) or unix:/run/substrate/api.sock")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--api-key-env", required=True)
    p.add_argument("--workroot", required=True)
    p.add_argument("--context-window", type=int, default=32768)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--role-dir", default=str(DEFAULT_ROLE_DIR))
    p.add_argument("--status-dir", help="write worker-<id>.json heartbeats here (for the live status page)")
    args = p.parse_args(argv)

    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True
        print(f"worker {args.id}: stop requested; finishing current job", flush=True)

    signal.signal(signal.SIGTERM, on_term)
    cfg = WorkerConfig(args.base_url, args.model, args.api_key_env, args.context_window, args.max_tokens,
                       Path(args.role_dir), args.id, Path(args.status_dir) if args.status_dir else None)
    sub = connect(args.substrate)
    rng = random.Random()
    failures = 0
    while not stopping:
        try:
            outcome = run_job(sub, rng=rng, cfg=cfg, workroot=Path(args.workroot))
            print(f"worker {args.id}: {outcome}", flush=True)
            failures = failures + 1 if outcome == "failed" else 0
        except LookupError as e:  # no island/program yet
            print(f"worker {args.id}: waiting ({e})", flush=True)
            write_heartbeat(cfg.status_dir, args.id, state="waiting", last_summary=str(e))
            failures = 0
            time.sleep(10)
        delay = backoff_seconds(failures)
        if delay:
            print(f"worker {args.id}: {failures} failures in a row; backing off {delay:.0f}s", flush=True)
            end = time.monotonic() + delay
            beat_at = 0.0
            while not stopping and time.monotonic() < end:
                if time.monotonic() >= beat_at:  # keep the heartbeat fresh through a long backoff
                    write_heartbeat(cfg.status_dir, args.id, state="backoff", last_outcome="failed")
                    beat_at = time.monotonic() + 30
                time.sleep(0.5)
    print(f"worker {args.id}: finished current job, exiting", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
