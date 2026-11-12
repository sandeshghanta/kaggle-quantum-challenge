"""Islands and parent selection, derived from substrate events."""

from __future__ import annotations

import math
import random
import statistics

from loop import config
from substrate.cli import Substrate


def islands(sub: Substrate) -> dict[str, dict]:
    out: dict[str, dict] = {}
    events, _ = sub.since(None, types=["island.created", "island.updated"], limit=-1)
    for ev in events:
        p = ev.payload
        if ev.type == "island.created":
            out[p["island"]] = {"idea": p["idea"], "instructions": p["instructions"],
                                "weight": p.get("weight", 1.0), "status": "active"}
        elif p["island"] in out:
            out[p["island"]].update({k: p[k] for k in ("instructions", "weight", "status") if k in p})
    return out


def members(sub: Substrate, island: str) -> list[str]:
    seen: dict[str, None] = {}
    for ev in sub.since(None, types=["island.joined"], limit=-1)[0]:
        if ev.payload["island"] == island:
            seen.setdefault(ev.payload["program"], None)
    return list(seen)


def children(sub: Substrate, program: str) -> int:
    return len(sub.history("prog:" + program, types=["attempt.announced"]))


def _best_costs(sub: Substrate) -> dict[str, float]:
    best: dict[str, float] = {}
    for c in sub.frontier(config.BENCHMARK, cost_basis=config.COST_BASIS, k=10_000):
        if c.program not in best or c.cost < best[c.program]:
            best[c.program] = c.cost
    return best


def island_best(sub: Substrate, island: str) -> float | None:
    best = _best_costs(sub)
    costs = [best[p] for p in members(sub, island) if p in best]
    return min(costs) if costs else None


def island_of(sub: Substrate, program: str) -> str | None:
    joined = sub.history("prog:" + program, types=["island.joined"])
    return joined[0].payload["island"] if joined else None


def pick_island(sub: Substrate, rng: random.Random) -> str:
    active = {k: v for k, v in islands(sub).items() if v["status"] == "active" and v["weight"] > 0}
    if not active:
        raise LookupError("no active islands")
    names = sorted(active)
    return rng.choices(names, weights=[active[n]["weight"] for n in names])[0]


def pick_parent(sub: Substrate, island: str, rng: random.Random, *, lam: float = 10.0) -> str:
    progs = members(sub, island)
    if not progs:
        raise LookupError(f"island {island!r} has no programs")
    best = _best_costs(sub)
    scored = [p for p in progs if p in best]
    if not scored:
        return rng.choice(progs)
    costs = [best[p] for p in scored]
    median = statistics.median(costs)
    mad = 1.4826 * statistics.median(abs(c - median) for c in costs)
    weights = []
    for p in progs:
        if p in best:
            z = (median - best[p]) / mad if mad > 0 else 0.0
            w = 1.0 / (1.0 + math.exp(-lam * z))
        else:
            w = 0.05
        weights.append(w / (1 + children(sub, p)))
    return rng.choices(progs, weights=weights)[0]
