"""researcher.json for the status page: everything the researcher decided, derived from the substrate by code.

Keys (shapes as in dashboard/snapshot.example.json): generated_at, current_direction, islands, decisions,
decision_summary, beliefs, questions, researcher. Live and worker data (score, history, marks, health.assistants,
health.verifier) belong to the separate live updater's live.json and are deliberately absent. Pure function of the
log, `now` and `next_visit`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from loop import digest
from loop.digest import DAY, events, iso, parse_ts
from substrate.cli import Substrate

DECISIONS_SHOWN = 20


def _prediction_text(pr: dict) -> str:
    scope = f"island {pr['island']}" if pr["metric"] == "island_best_cost" else "benchmark"
    return f"{scope} best cost below {pr['threshold']:,.0f} within {pr['by_attempts']} attempts"





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











def build_researcher_view(sub: Substrate, *, now: datetime, cap_pct: float = 80,
                          next_visit: datetime | None = None) -> dict:
    now = now.astimezone(UTC)
    meas = digest.measurements(sub)
    attempts = events(sub, "attempt.announced")
    in_island = digest.program_islands(sub)
    dirs = digest.directions(sub, meas, attempts, in_island)
    best = min(meas, key=lambda m: m.cost) if meas else None
    quota = digest.latest_quota(sub) or {}
    visits = events(sub, "researcher.visited")
    visit_tokens: dict[str, int] = {}
    for ev in events(sub, "budget.spent"):
        if ev.payload["pool"] == "claude" and ev.cause:
            visit_tokens[ev.cause] = visit_tokens.get(ev.cause, 0) + ev.payload["tokens_in"] + ev.payload["tokens_out"]

    island_rows = digest.build_digest_islands(sub, now, meas, attempts, in_island)
    rows = [{"id": r["id"], "idea": r["idea"], "best_cost": r["best_cost"], "trend_pct": r["trend_24h_pct"],
             "call": "retired" if r["status"] == "retired" else _call(sub, r["id"])} for r in island_rows]

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
        active = [r for r in island_rows if r["status"] == "active"]
        total = sum(r["weight"] for r in active)
        on_best = sum(r["weight"] for r in active if r["id"] == best_island)
        current = {"id": labels[d["id"]], "t": iso(d["t"]), "brief": d["brief"], "why": d["why"],
                   "prediction": _prediction_text(d["prediction"]),
                   "progress": f"{d['attempts_used']} / {d['prediction']['by_attempts']} attempts",
                   "explore_share": round(1 - on_best / total, 2) if total and best_island else None}

    lessons = events(sub, "lessons.published")
    beliefs = lessons[-1].payload if lessons else {}
    return {
        "generated_at": iso(now),
        "current_direction": current,
        "islands": rows,
        "decisions": decisions,
        "decision_summary": {"closed": len(closed), "paid_off": len(paid),
                             "tokens_per_improvement": round(tokens / len(paid)) if paid else None},
        "beliefs": {"working": beliefs.get("working", []), "not_working": beliefs.get("not_working", []),
                    "open_questions": beliefs.get("open_questions", [])},
        "questions": [{"t": q["t"], "text": q["question"]} for q in digest.open_questions(sub, now)],
        "researcher": {"five_hour_pct": quota.get("five_hour_pct"), "seven_day_pct": quota.get("seven_day_pct"),
                       "cap_pct": cap_pct, "visits_24h": sum(1 for v in visits if parse_ts(v.ts) >= now - DAY),
                       "next_visit": iso(next_visit) if next_visit else None,
                       "last_visit": visits[-1].ts if visits else None},
    }


def write_view(path: Path, snapshot: dict) -> None:
    """Atomic write so a static file server never serves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2) + "\n")
    os.replace(tmp, path)
