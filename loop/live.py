"""Live updater: plain code, no model. Reads the substrate (read-only), the worker heartbeats and the machine
metrics, and writes live.json (every tick) and history.json (every 5 minutes) for the status page. It never
touches researcher.json, and nothing it computes (hardware samples, alerts) goes into the substrate."""

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
from loop.metrics import History, Monitor, metrics_url
from substrate import connect

REFERENCE_COST = 2076774
PUBLIC_BEST_RATIO = 15.2
TOP_RATIO = 20.6
STALE_AFTER = timedelta(minutes=2)
WINDOW = timedelta(hours=24)
ACTIVITY_LIMIT = 25
TYPES = ["program.committed", "island.joined", "attempt.announced", "candidate.measured", "candidate.rejected",
         "budget.spent", "direction", "lessons.published", "quota.observed", "researcher.visited", "attempt.abandoned"]

# progress verdict
IMPROVING_WITHIN = timedelta(hours=6)
PLATEAU_AFTER = timedelta(hours=24)
# alert thresholds
WORKER_DOWN_AFTER = timedelta(minutes=5)
QWEN_DOWN_AFTER = timedelta(minutes=2)
BACKLOG_MAX, OLDEST_WAIT_MAX_MIN = 20, 30
RESEARCHER_STUCK_AFTER = timedelta(hours=2)
CLAUDE_CAP_PCT = 80
DISK_MAX_PCT = 85
SPARK_MIN_TPS, SPARK_WINDOW = 70, timedelta(minutes=15)
CPU_MIN_PCT, CPU_WINDOW = 50, timedelta(minutes=30)
CLAUDE_MIN_PROJECTED_PCT = 60
WEEK = timedelta(days=7)


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


def progress(history: list[dict], now: datetime) -> dict:
    """Is the best still improving? A fixed rule on the new-best history, no model.

    improving: a new best within IMPROVING_WITHIN; plateau: none for PLATEAU_AFTER; slowing: in between.
    """
    bests = [h for h in history if h.get("new")]
    if not bests:
        return {"verdict": None, "last_gain": None, "per_day": []}
    last = _parse(bests[-1]["t"])
    since = now - last
    verdict = "improving" if since <= IMPROVING_WITHIN else "plateau" if since >= PLATEAU_AFTER else "slowing"
    today = now.date()
    per_day = []
    for back in range(6, -1, -1):
        day = today - timedelta(days=back)
        per_day.append({"day": day.isoformat(),
                        "count": sum(1 for h in bests[1:] if _parse(h["t"]).date() == day)})  # the first is the seed
    return {"verdict": verdict, "last_gain": bests[-1]["t"] if len(bests) > 1 else None,
            "since_hours": round(since.total_seconds() / 3600, 1), "per_day": per_day}


def claude_usage(quota: list, now: datetime) -> dict:
    """Plan usage over the last week from quota.observed, plus where the weekly window is headed."""
    pts = [{"t": _iso(t), "five_hour_pct": p.get("five_hour_pct"), "seven_day_pct": p.get("seven_day_pct")}
           for t, p in quota if now - t <= WEEK]
    out = {"points": pts, "latest": None, "projected_week_pct": None}
    if not quota:
        return out
    t, p = quota[-1]
    out["latest"] = {"t": _iso(t), **{k: p.get(k) for k in ("five_hour_pct", "seven_day_pct", "five_hour_resets_at",
                                                              "seven_day_resets_at")}}
    resets = p.get("seven_day_resets_at")
    if resets and p.get("seven_day_pct") is not None and _parse(resets) > now:
        elapsed = (t - (_parse(resets) - WEEK)) / WEEK
        if elapsed >= 1 / 7:  # the first day of a week says too little about the rest
            out["projected_week_pct"] = round(min(100.0, p["seven_day_pct"] / elapsed), 1)
    return out


def alerts(*, workers: list, verifier: dict, last_visit: datetime | None, claude: dict, monitor: Monitor | None,
           now: datetime) -> list[dict]:
    """The eight alert rules. Each fires while its condition holds; there is no history and no notification."""
    out = []

    def fire(rule, text):
        out.append({"rule": rule, "text": text})

    for w in workers:
        age = now - _parse(w["updated_at"])
        if age >= WORKER_DOWN_AFTER:
            fire("worker_down", f"Worker {w.get('worker')}: no heartbeat for {age.total_seconds() / 60:.0f} min")
    if monitor is not None and monitor.qwen_down_since and now - monitor.qwen_down_since >= QWEN_DOWN_AFTER:
        fire("qwen_unreachable", f"Qwen unreachable for {(now - monitor.qwen_down_since).total_seconds() / 60:.0f} min")
    if verifier["backlog"] > BACKLOG_MAX or verifier["oldest_wait_min"] > OLDEST_WAIT_MAX_MIN:
        fire("verifier_stuck", f"Verifier backlog {verifier['backlog']}, oldest waiting "
                               f"{verifier['oldest_wait_min']:.0f} min")
    latest = claude.get("latest") or {}
    under_cap = all((latest.get(k) or 0) < CLAUDE_CAP_PCT for k in ("five_hour_pct", "seven_day_pct"))
    if last_visit is not None and now - last_visit >= RESEARCHER_STUCK_AFTER and under_cap:
        fire("researcher_stuck", f"No researcher visit in {(now - last_visit).total_seconds() / 3600:.1f} h")
    if monitor is not None:
        disk = monitor.latest().get("disk_pct")
        if disk is not None and disk > DISK_MAX_PCT:
            fire("disk_full", f"Disk {disk:.0f}% full")
        if monitor.qwen_down_since is None:
            tps = monitor.average("qwen_tps", SPARK_WINDOW, now)
            if tps is not None and tps < SPARK_MIN_TPS:
                fire("spark_underused", f"Spark underused: {tps:.0f} tokens/s over 15 min")
        cpu = monitor.average("cpu_pct", CPU_WINDOW, now)
        if cpu is not None and cpu < CPU_MIN_PCT:
            fire("devbox_underused", f"Devbox underused: CPU {cpu:.0f}% over 30 min")
    proj = claude.get("projected_week_pct")
    if proj is not None and proj < CLAUDE_MIN_PROJECTED_PCT:
        fire("claude_underused", f"Claude underused: on pace for {proj:.0f}% of the week (target {CLAUDE_CAP_PCT}%)")
    return out


def build_live(sub, *, heartbeats_dir: Path | None, now: datetime, monitor: Monitor | None = None) -> dict:
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
    hour_ago = now - timedelta(hours=1)
    island_attempts: dict[str, int] = {}
    judged_1h: list[tuple[str, bool]] = []  # (program, passed) judged in the last hour
    island_best: dict[str, float] = {}
    quota: list = []
    last_visit = None

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
            if ts >= hour_ago:
                island_attempts[p["island"]] = island_attempts.get(p["island"], 0) + 1
        elif ev.type == "candidate.measured":
            judged.add(p["program"])
            full_checks += recent
            if recent:
                judged_24h.add(p["program"])
            counted = p.get("passes") and p.get("verifier") == tag and p.get("cost_basis") == basis
            if p.get("passes"):
                passing.add(p["program"])
            if ts >= hour_ago:
                judged_1h.append((p["program"], bool(p.get("passes"))))
            if counted:
                isl = island_of.get(p["program"])
                if isl is not None and p["cost"] < island_best.get(isl, float("inf")):
                    island_best[isl] = p["cost"]
            kind, text = "measured", f"cost {p['cost']:,.0f} · passes {'yes' if p.get('passes') else 'no'}"
            if counted and (best is None or p["cost"] < best):
                best = p["cost"]
                history.append({"t": t, "cost": best, "new": True})
                kind, text = "new best", f"cost {best:,.0f} · {REFERENCE_COST / best:.2f}x reference"
            if ts <= cutoff:
                best_at_cutoff = best
            activity.append({"t": t, "kind": kind, "text": text})
        elif ev.type == "candidate.rejected":
            judged.add(p["program"])
            if ts >= hour_ago:
                judged_1h.append((p["program"], False))
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
        elif ev.type == "attempt.abandoned":
            detail = _first_line(p.get("detail"))
            activity.append({"t": t, "kind": "abandoned",
                             "text": f"{p['island']}: {p['reason']}" + (f": {detail}" if detail else "")})
        elif ev.type == "quota.observed":
            quota.append((ts, p))
        elif ev.type == "researcher.visited":
            last_visit = ts
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
    verifier = {"backlog": len(unjudged),
                "oldest_wait_min": (now - _parse(unjudged[0].ts)).total_seconds() / 60 if unjudged else 0,
                "full_checks_24h": full_checks}
    claude = claude_usage(quota, now)
    islands = sorted(set(island_attempts) | set(island_best) | {island_of[p] for p, _ in judged_1h if p in island_of})
    effort = []
    for isl in islands:
        js = [ok for prog, ok in judged_1h if island_of.get(prog) == isl]
        effort.append({"island": isl, "attempts_1h": island_attempts.get(isl, 0), "judged_1h": len(js),
                       "pass_rate_1h": _rate(sum(js), len(js)), "best_cost": island_best.get(isl)})
    hw = None
    if monitor is not None:
        hw = {"now": monitor.latest(), "mem_total_gib": monitor.mem_total_gib,
              "qwen_down_since": _iso(monitor.qwen_down_since) if monitor.qwen_down_since else None}

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
            "verifier": verifier,
            "spend": {"qwen_tokens_out_24h": qwen_out, "cpu_seconds_24h": round(cpu, 2)},
        },
        "workers": workers,
        "activity": activity[::-1][:ACTIVITY_LIMIT],
        "progress": progress(history, now),
        "islands": effort,
        "claude": claude,
        "last_visit": _iso(last_visit) if last_visit else None,
        "hardware": hw,
        "alerts": alerts(workers=workers, verifier=verifier, last_visit=last_visit, claude=claude, monitor=monitor,
                         now=now),
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
    p.add_argument("--history", help="where to write history.json (5-minute machine metrics, last 7 days)")
    p.add_argument("--qwen-base-url", help="Qwen's OpenAI-compatible base URL; its /metrics is sampled")
    p.add_argument("--disk-path", help="filesystem whose usage is reported (the data directory)")
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
    monitor = Monitor(qwen_url=metrics_url(args.qwen_base_url) if args.qwen_base_url else None,
                      disk_path=Path(args.disk_path) if args.disk_path else None)
    history = History(Path(args.history) if args.history else None)
    while not stopping:
        now = datetime.now(UTC)
        try:
            history.add(now, monitor.sample(now), mem_total_gib=monitor.mem_total_gib)
            write_atomic(Path(args.out), build_live(sub, heartbeats_dir=hb, now=now, monitor=monitor))
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
