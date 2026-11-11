"""Read-type projections, computed from events on every query (SPEC §Read types)."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from substrate._store import Store
from substrate.types import BudgetState, Candidate, Event, Node


def _program_commits(store: Store) -> dict[str, Event]:
    """Earliest program.committed per program blob."""
    first: dict[str, Event] = {}
    for ev in store.since(None, types=["program.committed"], limit=-1)[0]:
        first.setdefault(ev.payload["blob"], ev)
    return first


def candidates(store: Store, benchmark: str) -> list[Candidate]:
    measured = [ev for ev in store.since(None, types=["candidate.measured"], limit=-1)[0]
                if ev.payload.get("benchmark") == benchmark]
    by_circuit: dict[str, list[Event]] = defaultdict(list)
    for ev in measured:
        by_circuit[ev.payload["circuit"]].append(ev)
    commits = _program_commits(store)
    out = []
    for circuit, evs in by_circuit.items():
        latest_verifier = evs[-1].payload.get("verifier")
        latest_basis = evs[-1].payload.get("cost_basis")
        kept = [e for e in evs if e.payload.get("verifier") == latest_verifier
                and e.payload.get("cost_basis") == latest_basis]
        worst = max(kept, key=lambda e: e.payload["cost"])
        program = worst.payload["program"]
        commit = commits.get(program)
        out.append(Candidate(
            circuit=circuit, program=program, benchmark=benchmark,
            cost=worst.payload["cost"],
            passes=all(e.payload["passes"] for e in kept),
            metrics=worst.payload.get("metrics", {}),
            verifier=latest_verifier, cost_basis=latest_basis,
            measurements=len(kept), first_seq=kept[0].seq, last_seq=kept[-1].seq,
            parent=commit.payload.get("parent") if commit else None,
            cause=commit.cause if commit else None,
            note=commit.payload.get("note") if commit else None,
        ))
    return sorted(out, key=lambda c: c.first_seq)


def frontier(store: Store, benchmark: str, *, k: int = 20, verifier: str | None = None,
             cost_basis: str | None = None) -> list[Candidate]:
    measured = [ev for ev in store.since(None, types=["candidate.measured"], limit=-1)[0]
                if ev.payload.get("benchmark") == benchmark]
    if not measured:
        return []
    if verifier is None:
        verifier = measured[-1].payload.get("verifier")
    under = [ev for ev in measured if ev.payload.get("verifier") == verifier]
    if not under:
        return []
    basis = cost_basis if cost_basis is not None else under[-1].payload.get("cost_basis")
    ranked = [c for c in candidates(store, benchmark)
              if c.passes and c.verifier == verifier and c.cost_basis == basis]
    return sorted(ranked, key=lambda c: (c.cost, c.first_seq))[:k]


def _strip(ref: str) -> str:
    return ref[len("prog:"):] if ref.startswith("prog:") else ref


def _best_costs(store: Store, benchmarks: set[str]) -> dict[str, float]:
    """Lowest passing Candidate cost per program, one pass per benchmark."""
    best: dict[str, float] = {}
    for bench in benchmarks:
        for c in candidates(store, bench):
            if c.passes and (c.program not in best or c.cost < best[c.program]):
                best[c.program] = c.cost
    return best


def _node(ev: Event, best: dict[str, float]) -> Node:
    program = ev.payload["blob"]
    return Node(program=program, parent=ev.payload.get("parent"), cause=ev.cause,
                benchmark=ev.payload["benchmark"], note=ev.payload.get("note"), seq=ev.seq,
                best_cost=best.get(program))


def node(store: Store, program: str) -> Node | None:
    ev = _program_commits(store).get(_strip(program))
    return _node(ev, _best_costs(store, {ev.payload["benchmark"]})) if ev else None


def lineage(store: Store, ref: str, *, direction: str = "up") -> list[Node]:
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    commits = _program_commits(store)
    start = _strip(ref)
    if direction == "up":
        chain, seen = [], {start}
        parent = commits[start].payload.get("parent") if start in commits else None
        while parent and parent in commits and parent not in seen:
            seen.add(parent)
            chain.append(commits[parent])
            parent = commits[parent].payload.get("parent")
        best = _best_costs(store, {ev.payload["benchmark"] for ev in chain})
        return [_node(ev, best) for ev in chain]
    children: dict[str, list[str]] = defaultdict(list)
    for blob, ev in commits.items():
        if ev.payload.get("parent"):
            children[ev.payload["parent"]].append(blob)
    found, stack = set(), [start]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found and child != start:
                found.add(child)
                stack.append(child)
    best = _best_costs(store, {commits[b].payload["benchmark"] for b in found})
    return sorted((_node(commits[b], best) for b in found), key=lambda n: n.seq)


_WINDOW = re.compile(r"^(\d+)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def budget(store: Store, pool: str, *, window: str = "7d", now: datetime | None = None) -> BudgetState:
    m = _WINDOW.match(window)
    if not m:
        raise ValueError(f"window must look like '30m', '5h' or '7d', got {window!r}")
    cutoff = (now or datetime.now(UTC)) - timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
    totals = {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0, "cpu_seconds": 0.0, "usd": 0.0}
    count = 0
    for ev in store.since(None, types=["budget.spent"], limit=-1)[0]:
        if ev.payload.get("pool") != pool:
            continue
        if datetime.fromisoformat(ev.ts.replace("Z", "+00:00")) < cutoff:
            continue
        count += 1
        for key in totals:
            totals[key] += ev.payload.get(key) or 0
    return BudgetState(pool=pool, window=window, events=count, **totals)
