"""Lab assistant worker: pick a parent, announce, run one pi job, gate the result, commit or abandon. Repeat.

Inside the job the assistant edits and runs its `check` tool until its idea passes or the budget runs out (time,
output tokens, number of checks; all from role.json). Afterwards the worker runs the same sample check itself and
commits only a program that passes with an estimate within the gate margin of the island's best; anything else is
recorded as attempt.abandoned so later assistants can steer away from it.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

import sys

from loop import config, pool, sandbox
from loop.check import check_bundle
from loop.heartbeat import JobProgress, write_heartbeat
from loop.pi_runner import run_pi, write_agent_dir
from loop.programs import pack, unpack
from substrate import connect
from substrate.cli import Substrate

DEFAULT_ROLE_DIR = config.REPO / "roles" / "lab_assistant" / "v2"
RECENT_FAILURES = 5
RECENT_SUCCESSES = 5
IN_PROGRESS_WINDOW_S = 20 * 60  # a plan older than the longest job is stale even if its job left no end event


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


def _first_line(text: str | None, limit: int = 200) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1][:limit] if lines and lines[0].startswith("Traceback") else (lines[0][:limit] if lines else "")


def recent_failures(sub: Substrate, island: str, n: int = RECENT_FAILURES) -> str:
    """The island's last `n` dead ends: abandoned jobs and verifier rejections, newest first, one line each."""
    notes = {ev.payload["blob"]: ev.payload.get("note") for ev in sub.since(None, types=["program.committed"],
                                                                             limit=-1)[0]}
    rows = []
    for ev in reversed(sub.since(None, types=["attempt.abandoned", "candidate.rejected"], limit=-1)[0]):
        p = ev.payload
        if ev.type == "attempt.abandoned":
            if p["island"] != island:
                continue
            rows.append(f"- {p['reason']}: {p.get('note') or '(no note)'} -- {_first_line(p.get('detail'))}")
        else:
            if p["reason"] == "duplicate" or pool.island_of(sub, p["program"]) != island:
                continue
            rows.append(f"- verifier {p['reason']}: {notes.get(p['program']) or '(no note)'} -- "
                        f"{_first_line(p.get('detail'))}")
        if len(rows) == n:
            break
    return "\n".join(rows) or "(none)"


def in_progress(sub: Substrate, island: str, *, now_ts: str | None = None, exclude: str | None = None) -> str:
    """Plans declared on this island by jobs that have not ended (no commit or abandonment caused by the same
    announcement) within the last IN_PROGRESS_WINDOW_S."""
    from datetime import UTC, datetime, timedelta
    now = datetime.fromisoformat(now_ts.replace("Z", "+00:00")) if now_ts else datetime.now(UTC)
    cutoff = now - timedelta(seconds=IN_PROGRESS_WINDOW_S)
    ended = {ev.cause for ev in sub.since(None, types=["program.committed", "attempt.abandoned"], limit=-1)[0]}
    rows = []
    for ev in reversed(sub.since(None, types=["attempt.planned"], limit=-1)[0]):
        if datetime.fromisoformat(ev.ts.replace("Z", "+00:00")) < cutoff:
            break
        if ev.payload["island"] == island and ev.cause not in ended and ev.cause != exclude:
            rows.append(f"- {ev.payload['plan']}")
    return "\n".join(rows) or "(none)"


def recent_successes(sub: Substrate, island: str, n: int = RECENT_SUCCESSES) -> str:
    """The island's last `n` programs that passed the verifier, newest first: the assistant's note and the cost."""
    notes = {ev.payload["blob"]: ev.payload.get("note") for ev in sub.since(None, types=["program.committed"],
                                                                             limit=-1)[0]}
    rows, seen = [], set()
    for ev in reversed(sub.since(None, types=["candidate.measured"], limit=-1)[0]):
        p = ev.payload
        if not p.get("passes") or p["program"] in seen or pool.island_of(sub, p["program"]) != island:
            continue
        seen.add(p["program"])
        rows.append(f"- {notes.get(p['program']) or '(no note)'} -- cost {p['cost']:,.0f}")
        if len(rows) == n:
            break
    return "\n".join(rows) or "(none)"


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
    budget = role.get("budget", {"timeout_s": role.get("timeout_s", 900)})
    island = pool.pick_island(sub, rng)
    parent = pool.pick_parent(sub, island, rng)
    actor = config.actor("lab_assistant", role["version"], cfg.model)
    plan = f"{island}: improving parent {parent.removeprefix('sha256:')[:12]}"
    ann = sub.append("attempt.announced", {"island": island, "plan": plan},
                     subject="prog:" + parent, actor=actor, cause=pool.island_config_event(sub, island))
    job = Path(workroot) / ann
    progress = JobProgress(cfg.status_dir, cfg.worker_id, island=island, parent=parent, announcement=ann)
    outcome, summary = "failed", "crashed"
    checks, planned = 0, False

    def on_event(ev: dict) -> None:
        nonlocal checks, planned
        if ev.get("type") == "tool_execution_end" and ev.get("toolName") == "check":
            checks += 1
        if ev.get("type") == "tool_execution_start" and ev.get("toolName") == "plan" and not planned:
            idea = str((ev.get("args") or {}).get("idea") or "").strip()[:500]
            if idea:  # mirrors the tool: the first plan is the job's plan
                planned = True
                sub.append("attempt.planned", {"island": island, "plan": idea}, subject="prog:" + parent,
                           actor=actor, cause=ann)
        progress.on_event(ev)

    def abandon(reason: str, detail: str, note: str, estimate: float | None = None) -> str:
        nonlocal outcome, summary
        sub.append("attempt.abandoned", {"island": island, "reason": reason, "detail": detail[-2000:],
                                         "note": note, "estimate": estimate, "checks": checks},
                   subject="prog:" + parent, actor=actor, cause=ann)
        outcome, summary = "abandoned", f"{reason}: {note}"[:200]
        return "abandoned"

    try:
        bundle = job / "bundle"
        unpack(sub.get_blob(parent), bundle)
        before = pack(bundle)
        best = pool.island_best(sub, island)
        best_sweep = pool.island_best_sweep(sub, island) or best
        gate = best_sweep * (1 + role.get("gate_margin", 0.05)) if best_sweep else None
        prompt = (cfg.role_dir / "task.md").read_text().format(
            parent_cost=f"{best:.6g}" if best else "not yet measured",
            gate_cost=f"{gate:.6g}" if gate else "any (nothing measured yet)",
            island_instructions=pool.islands(sub)[island]["instructions"], lessons=_lessons(sub),
            in_progress=in_progress(sub, island, exclude=ann), recent_successes=recent_successes(sub, island),
            recent_failures=recent_failures(sub, island))
        agent = write_agent_dir(job / "agent", base_url=cfg.base_url, model=cfg.model, api_key_env=cfg.api_key_env,
                                context_window=cfg.context_window, max_tokens=cfg.max_tokens)
        run = run_pi(bundle, agent, prompt=prompt, system_append=cfg.role_dir / "system.md", tools=role["tools"],
                     model=cfg.model, timeout_s=budget["timeout_s"], on_event=on_event,
                     extensions=[cfg.role_dir / e for e in role.get("extensions", [])],
                     extra_ro=[config.REPO, *sandbox.python_paths()],
                     extra_env={"LOOP_PYTHON": sys.executable, "LOOP_REPO": str(config.REPO),
                                "LOOP_CHECK_MAX": str(budget.get("checks", 8))},
                     max_output_tokens=budget.get("output_tokens"))
        sub.append("budget.spent", {"pool": "qwen", "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
                                    "tokens_cached": run.tokens_cached, "wall_seconds": round(run.seconds, 2)},
                   subject="pool:qwen", actor=actor, cause=ann)
        after = pack(bundle)
        note = _note(run.final_text)
        if after == before:
            if not run.ok:
                outcome, summary = "failed", (run.error or "failed")[:200]
                return "failed"
            outcome, summary = "unchanged", note
            return "unchanged"
        out_of_budget = not run.ok and run.error in ("timeout", "token budget")
        rep = check_bundle(bundle)
        if not rep.passes:
            reason = "out_of_budget" if out_of_budget else ("build_failed" if rep.stage == "build" else "check_failed")
            return abandon(reason, rep.error or "", note if run.ok else f"(stopped: {run.error}) {note}")
        if gate is not None and rep.cost_estimate is not None and rep.cost_estimate > gate:
            return abandon("not_better", f"estimated cost {rep.cost_estimate:,.0f} > gate {gate:,.0f}", note,
                           rep.cost_estimate)
        program = sub.put_blob(after)
        # island.joined first: a crash between the two leaves an unjudged-but-placed program, never an orphan
        sub.append("island.joined", {"program": program, "island": island, "via": "submit"},
                   subject="prog:" + program, actor=actor, cause=ann)
        sub.append("program.committed", {"blob": program, "parent": parent, "benchmark": config.BENCHMARK,
                                         "note": note},
                   subject="prog:" + program, actor=actor, cause=ann)
        outcome, summary = "committed", note
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
