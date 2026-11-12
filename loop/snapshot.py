"""snapshot.json for the progress dashboard, derived from the substrate by code (no model).

Shape: dashboard/snapshot.example.json (with "example": false). Pure function of the log and `now`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loop import config, digest
from loop.digest import DAY, events, iso, parse_ts
from substrate.cli import Substrate

REFERENCE_COST = 2076774      # Google reference generator, as on the dashboard example (cost = Q x (Toffolis + T/4))
PUBLIC_BEST_RATIO = 15.2
TOP_RATIO = 20.6
HISTORY_POINTS = 200
DECISIONS_SHOWN = 20
JOB_TIMEOUT = timedelta(minutes=20)   # a lab assistant job is killed at 15 minutes; later than that it is not running


def _ratio(cost: float | None) -> float | None:
    return round(REFERENCE_COST / cost, 2) if cost else None


def _prediction_text(pr: dict) -> str:
    scope = f"island {pr['island']}" if pr["metric"] == "island_best_cost" else "benchmark"
    return f"{scope} best cost below {pr['threshold']:,.0f} within {pr['by_attempts']} attempts"


def _history(meas, now) -> list[dict]:
    points, best = [], None
    for m in meas:
        if best is None or m.cost < best:
            best = m.cost
            points.append({"t": iso(m.t), "cost": best})
    if best is not None:
        points.append({"t": iso(now), "cost": best})
    return points[-HISTORY_POINTS:]


def _call(sub, island: str) -> str:
    """The researcher's latest call on an island, from its most recent island event."""
    weight, call = None, "seed"
    for ev in events(sub, "island.created", "island.updated", "island.joined"):
        p = ev.payload
        if ev.type == "island.joined":
            if p["island"] == island and p.get("via") in ("restart", "migration"):
                call = "restarted" if p["via"] == "restart" else "migrated"
            continue
        if p["island"] != island:
            continue
        if ev.type == "island.created":
            call, weight = ("seed" if ev.actor.get("role") == "seed" else "new"), p.get("weight", 1.0)
        elif p.get("status") == "retired":
            call = "retired"
        elif "weight" in p:
            call = "boosted" if weight is None or p["weight"] > weight else "throttled"
            weight = p["weight"]
        else:
            call = "retuned"
    return call


def _in_flight(sub, now) -> dict[str, int]:
    """Announced attempts in the last JOB_TIMEOUT whose job has not yet recorded its spend, per island."""
    done = {ev.cause for ev in events(sub, "budget.spent") if ev.cause}
    out: dict[str, int] = {}
    for a in events(sub, "attempt.announced"):
        if parse_ts(a.ts) >= now - JOB_TIMEOUT and a.id not in done:
            out[a.payload["island"]] = out.get(a.payload["island"], 0) + 1
    return out


def _verifier_health(sub, now) -> dict:
    judged_events = events(sub, "candidate.measured", "candidate.rejected")
    judged = {ev.payload["program"] for ev in judged_events}
    waiting = [parse_ts(ev.ts) for ev in events(sub, "program.committed") if ev.payload["blob"] not in judged]
    return {"backlog": len(waiting),
            "oldest_wait_min": round((now - min(waiting)).total_seconds() / 60, 1) if waiting else 0.0,
            "full_checks_24h": sum(1 for ev in judged_events
                                   if ev.type == "candidate.measured" and parse_ts(ev.ts) >= now - DAY)}


def _sweep_fail_rate(sub, now) -> float | None:
    recent = [ev for ev in events(sub, "candidate.measured", "candidate.rejected") if parse_ts(ev.ts) >= now - DAY]
    swept = [ev for ev in recent if ev.type == "candidate.measured" or ev.payload.get("tier") in ("sweep", "gate")]
    failed = [ev for ev in swept if ev.type == "candidate.rejected" and ev.payload.get("tier") == "sweep"]
    return round(len(failed) / len(swept), 3) if swept else None


def build_snapshot(sub: Substrate, *, now: datetime, cap_pct: float = 80, interval_min: float = 45) -> dict:
    now = now.astimezone(UTC)
    meas = digest.measurements(sub)
    attempts = events(sub, "attempt.announced")
    in_island = digest.program_islands(sub)
    dirs = digest.directions(sub, meas, attempts, in_island)
    best = min(meas, key=lambda m: m.cost) if meas else None
    best_before = digest.best_cost(meas, at=now - DAY)

    quota = digest.latest_quota(sub) or {}
    visits = events(sub, "researcher.visited")
    visit_tokens = {}
    for ev in events(sub, "budget.spent"):
        if ev.payload["pool"] == "claude" and ev.cause:
            visit_tokens[ev.cause] = visit_tokens.get(ev.cause, 0) + ev.payload["tokens_in"] + ev.payload["tokens_out"]

    flight = _in_flight(sub, now)
    rows = []
    for isl in digest.build_digest_islands(sub, now, meas, attempts, in_island):
        rows.append({"id": isl["id"], "idea": isl["idea"], "assistants": flight.get(isl["id"], 0),
                     "best_cost": isl["best_cost"], "trend_pct": isl["trend_24h_pct"],
                     "call": "retired" if isl["status"] == "retired" else _call(sub, isl["id"])})

    hit = digest.hit_rate(sub, now, meas, attempts)
    labels = {d["id"]: f"D{i}" for i, d in enumerate(dirs, 1)}
    decisions = []
    for d in reversed(dirs[-DECISIONS_SHOWN:]):
        detail = {"met": f"new best {d['achieved']:,.0f}" if d["achieved"] else "met",
                  "missed": f"no gain in {d['attempts_used']} attempts",
                  "open": f"{d['attempts_used']} attempts so far"}[d["status"]]
        decisions.append({"id": labels[d["id"]], "t": iso(d["t"]), "brief": d["brief"],
                          "prediction": _prediction_text(d["prediction"]), "outcome": d["status"],
                          "detail": detail, "tokens": visit_tokens.get(d["cause"], 0)})
    closed = [d for d in dirs if d["status"] != "open"]
    paid = [d for d in closed if d["status"] == "met"]
    tokens = sum(visit_tokens.get(d["cause"], 0) for d in closed)

    current = None
    if dirs:
        d = dirs[-1]
        best_island = next(iter(in_island.get(best.program, ())), None) if best else None
        active = [r for r in digest.build_digest_islands(sub, now, meas, attempts, in_island) if r["status"] == "active"]
        total = sum(r["weight"] for r in active)
        on_best = sum(r["weight"] for r in active if r["id"] == best_island)
        current = {"id": labels[d["id"]], "t": iso(d["t"]), "brief": d["brief"], "why": d["why"],
                   "prediction": _prediction_text(d["prediction"]),
                   "progress": f"{d['attempts_used']} / {d['prediction']['by_attempts']} attempts",
                   "explore_share": round(1 - on_best / total, 2) if total and best_island else None}

    lessons = events(sub, "lessons.published")
    beliefs = lessons[-1].payload if lessons else {}
    last = parse_ts(visits[-1].ts) if visits else None
    return {
        "example": False,
        "generated_at": iso(now),
        "benchmark": config.BENCHMARK,
        "score": {"best_cost": best.cost if best else None, "Q": best.Q if best else None,
                  "T": best.T if best else None, "reference_cost": REFERENCE_COST,
                  "public_best_ratio": PUBLIC_BEST_RATIO, "top_ratio": TOP_RATIO,
                  "ratio_24h_ago": _ratio(best_before)},
        "history": _history(meas, now),
        "marks": [{"t": iso(d["t"]), "label": labels[d["id"]]} for d in dirs],
        "health": {
            "researcher": {"five_hour_pct": quota.get("five_hour_pct"), "seven_day_pct": quota.get("seven_day_pct"),
                           "cap_pct": cap_pct,
                           "visits_24h": sum(1 for v in visits if parse_ts(v.ts) >= now - DAY),
                           "next_visit": iso(last + timedelta(minutes=interval_min)) if last else iso(now)},
            "assistants": {"running": sum(flight.values()), "attempts_24h": hit["attempts"],
                           "hit_rate": hit["hit_rate"], "sweep_fail_rate": _sweep_fail_rate(sub, now)},
            "verifier": _verifier_health(sub, now),
        },
        "current_direction": current,
        "islands": rows,
        "decisions": decisions,
        "decision_summary": {"closed": len(closed), "paid_off": len(paid),
                             "tokens_per_improvement": round(tokens / len(paid)) if paid else None},
        "beliefs": {"working": beliefs.get("working", []), "not_working": beliefs.get("not_working", []),
                    "open_questions": beliefs.get("open_questions", [])},
        "questions": [{"t": q["t"], "text": q["question"]} for q in digest.open_questions(sub, now)],
    }


def write_snapshot(path: Path, snapshot: dict) -> None:
    """Atomic write so a static file server never serves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2) + "\n")
    os.replace(tmp, path)
