"""Read types returned by the API. Computed from events on every query, never stored."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    seq: int
    v: int
    id: str
    ts: str
    type: str
    type_v: int
    subject: str
    actor: dict
    cause: str | None
    payload: dict


@dataclass(frozen=True)
class Candidate:
    circuit: str
    program: str
    benchmark: str
    cost: float
    passes: bool
    metrics: dict
    verifier: str | None
    cost_basis: str | None
    measurements: int
    first_seq: int
    last_seq: int
    parent: str | None
    cause: str | None
    note: str | None


@dataclass(frozen=True)
class Node:
    program: str
    parent: str | None
    cause: str | None
    benchmark: str
    note: str | None
    seq: int
    best_cost: float | None


@dataclass(frozen=True)
class BudgetState:
    pool: str
    window: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cpu_seconds: float
    usd: float
    events: int
