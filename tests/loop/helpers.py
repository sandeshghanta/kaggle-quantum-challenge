"""Shared builders for researcher tests: a controllable clock and substrate fixtures."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from loop import config
from substrate import _ulid

BENCH = "bench:invsqrt"
LAB = {"role": "lab_assistant"}
RES = {"role": "researcher"}
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, monkeypatch, start: datetime = T0):
        self.t = start
        monkeypatch.setattr(_ulid, "utc_now_iso",
                            lambda: self.t.isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    def advance(self, **kw) -> datetime:
        self.t += timedelta(**kw)
        return self.t


def prog(key: str) -> str:
    """A valid program digest derived from any short key."""
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def island(sub, name, idea="idea", instructions="do x", weight=1.0):
    return sub.append("island.created", {"island": name, "idea": idea, "instructions": instructions,
                                         "weight": weight}, subject=BENCH, actor=RES)


def member(sub, ch, *, island="main", cost=None, Q=82, T=1000.0, actor=LAB, via="submit"):
    p = prog(ch)
    sub.append("program.committed", {"blob": p, "parent": None, "benchmark": "invsqrt"},
               subject="prog:" + p, actor=actor)
    sub.append("island.joined", {"program": p, "island": island, "via": via}, subject="prog:" + p, actor=actor)
    if cost is not None:
        measure(sub, ch, cost, Q=Q, T=T)
    return p


def measure(sub, ch, cost, *, Q=82, T=1000.0, passes=True):
    p = prog(ch)
    return sub.append("candidate.measured", {
        "program": p, "circuit": prog(f"circuit-{ch}-{cost}"), "cost": cost, "passes": passes,
        "benchmark": "invsqrt", "verifier": config.verifier_tag(), "cost_basis": config.COST_BASIS,
        "metrics": {"Q": Q, "T": T, "sweep_cost": cost}}, subject="prog:" + p, actor={"role": "verifier"})


def attempt(sub, island="main", parent="a", n=1):
    for _ in range(n):
        sub.append("attempt.announced", {"island": island, "plan": "p"}, subject="prog:" + prog(parent),
                   actor=LAB)


def direction(sub, *, island="main", threshold=100.0, by=5, metric="island_best_cost", cause=None):
    return sub.append("direction", {"brief": "b", "why": "w", "islands": [island],
                                    "prediction": {"metric": metric, "island": island if metric == "island_best_cost" else None,
                                                   "threshold": threshold, "by_attempts": by}},
                      subject=BENCH, actor=RES, cause=cause)
