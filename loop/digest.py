"""The researcher's digest: a compact, deterministic summary of the substrate, built by code.

`build_digest(sub, now=...)` is a pure function of the event log and `now`. The helpers here
(measurement history, prediction status, island rows) are shared with loop.snapshot.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loop import config, pool
from substrate.cli import Substrate

DAY = timedelta(days=1)
BENCH = "bench:" + config.BENCHMARK
INSTRUCTIONS_CHARS = 600
DETAIL_CHARS = 300
RECENT_DIRECTIONS = 12
QUESTION_WINDOW = 7 * DAY


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(UTC)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def events(sub: Substrate, *types: str) -> list:
    return sub.since(None, types=list(types), limit=-1)[0]


# ── measurements and islands ───────────────────────────────────────────
@dataclass(frozen=True)
class Measure:
    seq: int
    t: datetime
    program: str
    cost: float
    Q: float | None
    T: float | None


def measurements(sub: Substrate) -> list[Measure]:
    """Passing measurements under the current verifier and cost basis, in commit order."""
    tag = config.verifier_tag()
    out = []
    for ev in events(sub, "candidate.measured"):
        p = ev.payload
        if p["passes"] and p["verifier"] == tag and p["cost_basis"] == config.COST_BASIS:
            m = p.get("metrics") or {}
            out.append(Measure(ev.seq, parse_ts(ev.ts), p["program"], p["cost"], m.get("Q"), m.get("T")))
    return out


def program_islands(sub: Substrate) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for ev in events(sub, "island.joined"):
        out[ev.payload["program"]].add(ev.payload["island"])
    return out


def best_cost(meas: list[Measure], *, island: str | None = None, at: datetime | None = None,
              upto_seq: int | None = None, in_island: dict[str, set[str]] | None = None) -> float | None:
    costs = [m.cost for m in meas
             if (at is None or m.t <= at) and (upto_seq is None or m.seq <= upto_seq)
             and (island is None or island in (in_island or {}).get(m.program, ()))]
    return min(costs) if costs else None


def attempts_of(attempts: list, island: str | None, *, after_seq: int = 0, since: datetime | None = None) -> int:
    return sum(1 for a in attempts if a.seq > after_seq and (island is None or a.payload["island"] == island)
               and (since is None or parse_ts(a.ts) >= since))


# ── predictions ────────────────────────────────────────────────────────
def prediction_status(direction, meas: list[Measure], attempts: list, in_island: dict[str, set[str]]) -> dict:
    """met / missed / open, computed from events. Never from the model.

    Scope is the prediction's island (island_best_cost) or every island (bench_best_cost). The
    prediction is met if some passing measurement of an in-scope program, committed after the
    direction, has cost below the threshold and at most `by_attempts` attempts were announced in
    scope between the direction and that measurement. It is missed if `by_attempts` attempts have
    been announced in scope and it was not met. Otherwise it is open.
    """
    pr = direction.payload["prediction"]
    island = pr["island"] if pr["metric"] == "island_best_cost" else None
    scoped = [a for a in attempts if a.seq > direction.seq and (island is None or a.payload["island"] == island)]
    used = len(scoped)
    current = best_cost([m for m in meas if m.seq > direction.seq], island=island, in_island=in_island)
    for m in meas:
        if m.seq <= direction.seq or m.cost >= pr["threshold"]:
            continue
        if island is not None and island not in in_island.get(m.program, ()):
            continue
        if sum(1 for a in scoped if a.seq <= m.seq) <= pr["by_attempts"]:
            return {"status": "met", "attempts_used": used, "best_since": current, "achieved": m.cost}
    status = "missed" if used >= pr["by_attempts"] else "open"
    return {"status": status, "attempts_used": used, "best_since": current, "achieved": None}


def directions(sub: Substrate, meas: list[Measure], attempts: list, in_island: dict[str, set[str]]) -> list[dict]:
    """Every direction, oldest first, with its prediction status."""
    out = []
    for ev in events(sub, "direction"):
        p = ev.payload
        out.append({"id": ev.id, "seq": ev.seq, "t": parse_ts(ev.ts), "brief": p["brief"], "why": p["why"],
                    "prediction": p["prediction"], "islands": p.get("islands", []), "cause": ev.cause,
                    **prediction_status(ev, meas, attempts, in_island)})
    return out


# ── instructions, visits ───────────────────────────────────────────────
def last_visit(sub: Substrate):
    v = events(sub, "researcher.visited")
    return v[-1] if v else None


def unanswered_instructions(sub: Substrate) -> list:
    """human.instructed events committed after the latest researcher.visited."""
    visit = last_visit(sub)
    after = visit.seq if visit else 0
    return [ev for ev in events(sub, "human.instructed") if ev.seq > after]


# ── digest ─────────────────────────────────────────────────────────────
def _island_rows(sub, now, meas, attempts, in_island) -> list[dict]:
    rows = []
    for name, isl in pool.islands(sub).items():
        best, before = best_cost(meas, island=name, in_island=in_island), \
            best_cost(meas, island=name, at=now - DAY, in_island=in_island)
        rows.append({
            "id": name, "idea": isl["idea"], "instructions": isl["instructions"][:INSTRUCTIONS_CHARS],
            "weight": isl["weight"], "status": isl["status"], "members": len(pool.members(sub, name)),
            "best_cost": best, "best_sweep_cost": pool.island_best_sweep(sub, name),
            "attempts_24h": attempts_of(attempts, name, since=now - DAY),
            "trend_24h_pct": round((best - before) / before * 100, 2) if best is not None and before else None,
        })
    return rows


def _rejections(sub, now) -> dict:
    recent = [ev for ev in events(sub, "candidate.rejected") if parse_ts(ev.ts) >= now - DAY]
    samples, seen = [], set()
    for ev in reversed(recent):
        detail = (ev.payload.get("detail") or "").strip()[:DETAIL_CHARS]
        if detail and ev.payload["reason"] in ("build_failed", "quick_check_failed", "verify_error") \
                and detail not in seen:
            seen.add(detail)
            samples.append({"reason": ev.payload["reason"], "detail": detail})
        if len(samples) == 3:
            break
    return {"by_reason": dict(Counter(ev.payload["reason"] for ev in recent)), "samples": samples}


def hit_rate(sub: Substrate, now: datetime, meas: list[Measure], attempts: list) -> dict:
    """Programs committed by lab assistants in the last 24h that got a passing measurement, per attempt."""
    n_attempts = attempts_of(attempts, None, since=now - DAY)
    passing_programs = {m.program for m in meas}
    committed = [ev.payload["blob"] for ev in events(sub, "program.committed")
                 if ev.actor.get("role") == "lab_assistant" and parse_ts(ev.ts) >= now - DAY]
    passing = sum(1 for p in committed if p in passing_programs)
    return {"attempts": n_attempts, "committed": len(committed), "passing": passing,
            "hit_rate": round(passing / n_attempts, 4) if n_attempts else None}


def latest_quota(sub: Substrate) -> dict | None:
    q = events(sub, "quota.observed")
    return {"t": q[-1].ts, **q[-1].payload} if q else None


def spend(sub: Substrate, now: datetime, window: timedelta = DAY) -> dict:
    out: dict[str, dict] = {p: {"tokens_in": 0, "tokens_out": 0, "cpu_seconds": 0.0, "events": 0}
                            for p in ("claude", "qwen", "cpu")}
    for ev in events(sub, "budget.spent"):
        if parse_ts(ev.ts) < now - window or ev.payload["pool"] not in out:
            continue
        o = out[ev.payload["pool"]]
        o["tokens_in"] += ev.payload["tokens_in"]
        o["tokens_out"] += ev.payload["tokens_out"]
        o["cpu_seconds"] += ev.payload.get("cpu_seconds") or 0
        o["events"] += 1
    for o in out.values():
        o["cpu_seconds"] = round(o["cpu_seconds"], 1)
    return out


def build_digest(sub: Substrate, *, now: datetime) -> dict:
    now = now.astimezone(UTC)
    meas = measurements(sub)
    attempts = events(sub, "attempt.announced")
    in_island = program_islands(sub)
    visit = last_visit(sub)

    frontier = [{"cost": c.cost, "Q": c.metrics.get("Q"), "T": c.metrics.get("T"), "program": c.program,
                 "island": pool.island_of(sub, c.program)}
                for c in sub.frontier(config.BENCHMARK, verifier=config.verifier_tag(),
                                      cost_basis=config.COST_BASIS, k=5)]

    dirs = directions(sub, meas, attempts, in_island)
    shown = [d for d in dirs if d["status"] == "open"] + \
        [d for d in dirs if d["status"] != "open"][-RECENT_DIRECTIONS:]
    shown.sort(key=lambda d: d["seq"])
    direction_rows = [{"id": d["id"], "t": iso(d["t"]), "brief": d["brief"], "why": d["why"],
                       "prediction": d["prediction"], "status": d["status"], "attempts_used": d["attempts_used"],
                       "best_since": d["best_since"]} for d in shown]

    lessons = events(sub, "lessons.published")
    questions = [{"id": ev.id, "t": ev.ts, "question": ev.payload["question"],
                  "context": ev.payload.get("context", "")}
                 for ev in events(sub, "question.asked") if parse_ts(ev.ts) >= now - QUESTION_WINDOW][-10:]

    return {
        "now": iso(now),
        "last_visit": visit.ts if visit else None,
        "islands": _island_rows(sub, now, meas, attempts, in_island),
        "frontier": frontier,
        "directions": direction_rows,
        "rejections_24h": _rejections(sub, now),
        "lab_assistants_24h": hit_rate(sub, now, meas, attempts),
        "lessons": lessons[-1].payload if lessons else None,
        "human_instructions": [{"id": ev.id, "t": ev.ts, "kind": ev.payload["kind"], "text": ev.payload["text"]}
                               for ev in unanswered_instructions(sub)],
        "open_questions": questions,
        "budget": {"quota": latest_quota(sub), "spend_24h": spend(sub, now)},
    }
