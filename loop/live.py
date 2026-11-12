"""Live updater: plain code, no model. Reads the substrate (read-only) and the worker heartbeats and
writes live.json for the status page every few seconds. It never touches researcher.json."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loop import config
from substrate import connect

REFERENCE_COST = 2076774
PUBLIC_BEST_RATIO = 15.2
TOP_RATIO = 20.6
STALE_AFTER = timedelta(minutes=2)
WINDOW = timedelta(hours=24)
ACTIVITY_LIMIT = 25
TYPES = ["program.committed", "island.joined", "attempt.announced", "candidate.measured", "candidate.rejected",
         "budget.spent", "direction", "lessons.published"]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def _first_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0][:200] if lines else ""


def _workers(heartbeats_dir: Path | None, now: datetime) -> list[dict]:
    out = []
    if heartbeats_dir is not None and Path(heartbeats_dir).is_dir():
        for path in Path(heartbeats_dir).glob("worker-*.json"):
            try:
                hb = json.loads(path.read_text())
                stale = now - _parse(hb["updated_at"]) > STALE_AFTER
                out.append({**hb, "stale": stale})
            except (OSError, ValueError, KeyError, TypeError):
                continue  # half-written or foreign file: skip it, the next tick will see a good one
    return sorted(out, key=lambda w: w.get("worker", 0))


def build_live(sub, *, heartbeats_dir: Path | None, now: datetime) -> dict:
    tag, basis = config.verifier_tag(), config.COST_BASIS
    events, _ = sub.since(None, types=TYPES, limit=-1)
    cutoff = now - WINDOW

    island_of: dict[str, str] = {}
    committed: list = []
    judged: set[str] = set()
    passing: set[str] = set()
    best = None
    best_at_cutoff = None
    history: list[dict] = []
    marks: list[dict] = []
    activity: list[dict] = []
    attempts = full_checks = qwen_out = 0
    cpu = 0.0
    judged_24h: set[str] = set()
    sweep_failed_24h: set[str] = set()
    n_direction = 0

    for ev in events:
        p, ts = ev.payload, _parse(ev.ts)
        recent = ts >= cutoff
        t = _iso(ts)
        if ev.type == "island.joined":
            island_of.setdefault(p["program"], p["island"])
        elif ev.type == "program.committed":
            committed.append(ev)
            activity.append({"t": t, "kind": "committed", "program": p["blob"], "text": None, "_note": p.get("note")})
        elif ev.type == "attempt.announced":
            attempts += recent
        elif ev.type == "candidate.measured":
            judged.add(p["program"])
            full_checks += recent
            if recent:
                judged_24h.add(p["program"])
            counted = p.get("passes") and p.get("verifier") == tag and p.get("cost_basis") == basis
            if p.get("passes"):
                passing.add(p["program"])
            kind, text = "measured", f"cost {p['cost']:,.0f} · passes {'yes' if p.get('passes') else 'no'}"
            if counted and (best is None or p["cost"] < best):
                best = p["cost"]
                history.append({"t": t, "cost": best})
                kind, text = "new best", f"cost {best:,.0f} · {REFERENCE_COST / best:.2f}x reference"
            if ts <= cutoff:
                best_at_cutoff = best
            activity.append({"t": t, "kind": kind, "text": text})
        elif ev.type == "candidate.rejected":
            judged.add(p["program"])
            if recent:
                judged_24h.add(p["program"])
                if p.get("tier") == "sweep":
                    sweep_failed_24h.add(p["program"])
            detail = _first_line(p.get("detail"))
            activity.append({"t": t, "kind": "rejected",
                             "text": f"{p['reason']} ({p.get('tier', '?')})" + (f": {detail}" if detail else "")})
        elif ev.type == "budget.spent":
            if recent and p.get("pool") == "qwen":
                qwen_out += p.get("tokens_out") or 0
            if recent and p.get("pool") == "cpu":
                cpu += p.get("cpu_seconds") or 0
        elif ev.type == "direction":
            n_direction += 1
            marks.append({"t": t, "label": f"D{n_direction}"})
            activity.append({"t": t, "kind": "direction", "text": p["brief"]})
        elif ev.type == "lessons.published":
            activity.append({"t": t, "kind": "lessons",
                             "text": f"{len(p.get('working', []))} working, {len(p.get('not_working', []))} not working"})

    for a in activity:
        if a["kind"] == "committed":
            island = island_of.get(a.pop("program"), "?")
            a["text"] = f"{island}: {a.pop('_note') or '(no note)'}"

    top = sub.frontier("invsqrt", k=1, verifier=tag, cost_basis=basis)
    top = top[0] if top else None
    history.append({"t": _iso(now), "cost": best}) if best is not None else None

    hits = sum(1 for ev in committed if ev.payload.get("parent") is not None
               and _parse(ev.ts) >= cutoff and ev.payload["blob"] in passing)
    unjudged = [ev for ev in committed if ev.payload["blob"] not in judged]
    workers = _workers(heartbeats_dir, now)

    return {
        "generated_at": _iso(now),
        "score": {
            "best_cost": top.cost if top else None,
            "Q": top.metrics.get("Q") if top else None,
            "T": top.metrics.get("T") if top else None,
            "reference_cost": REFERENCE_COST,
            "public_best_ratio": PUBLIC_BEST_RATIO,
            "top_ratio": TOP_RATIO,
            "ratio_24h_ago": REFERENCE_COST / best_at_cutoff if best_at_cutoff else None,
        },
        "history": history,
        "marks": marks,
        "health": {
            "assistants": {"running": sum(1 for w in workers if not w["stale"]), "attempts_24h": attempts,
                           "hit_rate": _rate(hits, attempts),
                           "sweep_fail_rate": _rate(len(sweep_failed_24h), len(judged_24h))},
            "verifier": {"backlog": len(unjudged),
                         "oldest_wait_min": (now - _parse(unjudged[0].ts)).total_seconds() / 60 if unjudged else 0,
                         "full_checks_24h": full_checks},
            "spend": {"qwen_tokens_out_24h": qwen_out, "cpu_seconds_24h": round(cpu, 2)},
        },
        "workers": workers,
        "activity": activity[::-1][:ACTIVITY_LIMIT],
    }


def write_atomic(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, allow_nan=False))
    os.chmod(tmp, 0o644)  # the status page runs as another user
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--substrate", "--root", dest="substrate", required=True,
                   help="data directory (in-process) or unix:/run/substrate/api.sock")
    p.add_argument("--heartbeats", help="directory of worker-<id>.json heartbeats")
    p.add_argument("--out", required=True, help="where to write live.json")
    p.add_argument("--interval-s", type=float, default=15.0)
    p.add_argument("--once", action="store_true", help="write once and exit")
    args = p.parse_args(argv)

    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True
        print("live: stop requested", flush=True)

    signal.signal(signal.SIGTERM, on_term)
    sub = connect(args.substrate)
    hb = Path(args.heartbeats) if args.heartbeats else None
    while not stopping:
        try:
            write_atomic(Path(args.out), build_live(sub, heartbeats_dir=hb, now=datetime.now(UTC)))
        except Exception as e:  # keep the last good file; try again next tick
            print(f"live: update failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if args.once:
            break
        end = time.monotonic() + args.interval_s
        while not stopping and time.monotonic() < end:
            time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
