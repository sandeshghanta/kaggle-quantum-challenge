import json
from datetime import timedelta
from pathlib import Path

import pytest

from loop import snapshot
from substrate.cli import Substrate
from tests.loop import helpers as h

EXAMPLE = json.loads(Path("dashboard/snapshot.example.json").read_text())


def shape(d):
    """Keys at the top two levels."""
    return {k: (sorted(v) if isinstance(v, dict) else None) for k, v in d.items()}


@pytest.fixture
def clock(monkeypatch):
    return h.Clock(monkeypatch)


@pytest.fixture
def populated(tmp_path, clock):
    s = Substrate(tmp_path / "s")
    h.island(s, "main", idea="reference")
    h.island(s, "poly", idea="polynomial")
    h.member(s, "a", cost=2000000.0, Q=260, T=7735.0)
    clock.advance(hours=30)
    visit = s.append("researcher.visited", {"trigger": "schedule", "digest": h.prog("d"), "model": "m"},
                     subject=h.BENCH, actor=h.RES)
    s.append("budget.spent", {"pool": "claude", "tokens_in": 30000, "tokens_out": 1000}, subject="pool:claude",
             actor=h.RES, cause=visit)
    d = h.direction(s, island="main", threshold=1500000.0, by=10, cause=visit)
    s.append("island.updated", {"island": "poly", "weight": 0.2}, subject=h.BENCH, actor=h.RES, cause=d)
    clock.advance(minutes=5)
    h.attempt(s, n=2)
    h.member(s, "b", cost=1400000.0, Q=200, T=5000.0)
    s.append("lessons.published", {"working": ["w"], "not_working": ["n"], "open_questions": ["o"]},
             subject=h.BENCH, actor=h.RES, cause=visit)
    s.append("question.asked", {"question": "Q?"}, subject=h.BENCH, actor=h.RES, cause=visit)
    s.append("quota.observed", {"five_hour_pct": 40.0, "seven_day_pct": 22.0}, subject="pool:claude", actor=h.RES)
    return s


def test_shape_matches_example(populated, clock):
    snap = snapshot.build_snapshot(populated, now=clock.t)
    assert shape(snap) == shape(EXAMPLE)
    assert snap["example"] is False
    for row_key in ("islands", "decisions", "history", "marks", "questions"):
        assert set(snap[row_key][0]) == set(EXAMPLE[row_key][0]), row_key
    assert set(snap["current_direction"]) == set(EXAMPLE["current_direction"])
    json.dumps(snap, allow_nan=False)


def test_values(populated, clock):
    snap = snapshot.build_snapshot(populated, now=clock.t)
    assert snap["score"]["best_cost"] == 1400000.0 and snap["score"]["Q"] == 200
    assert snap["score"]["ratio_24h_ago"] == round(snapshot.REFERENCE_COST / 2000000.0, 2)
    assert snap["history"][0]["cost"] == 2000000.0 and snap["history"][-1]["cost"] == 1400000.0
    assert snap["marks"][0]["label"] == "D1"
    [dec] = snap["decisions"]
    assert dec["id"] == "D1" and dec["outcome"] == "met" and dec["tokens"] == 31000
    assert "island main best cost below 1,500,000" in dec["prediction"]
    assert snap["decision_summary"] == {"closed": 1, "paid_off": 1, "tokens_per_improvement": 31000}
    isl = {r["id"]: r for r in snap["islands"]}
    assert isl["poly"]["call"] == "throttled" and isl["main"]["call"] == "new" and isl["poly"]["best_cost"] is None
    assert isl["main"]["assistants"] == 2
    assert snap["current_direction"]["progress"] == "2 / 10 attempts"
    assert snap["current_direction"]["explore_share"] == round(0.2 / 1.2, 2)
    r = snap["health"]["researcher"]
    assert r["five_hour_pct"] == 40.0 and r["cap_pct"] == 80 and r["visits_24h"] == 1
    assert r["next_visit"] == "2026-11-12T18:45:00Z"
    assert snap["beliefs"] == {"working": ["w"], "not_working": ["n"], "open_questions": ["o"]}
    assert [q["text"] for q in snap["questions"]] == ["Q?"]
    assert snap["health"]["verifier"]["backlog"] == 0 and snap["health"]["assistants"]["attempts_24h"] == 2


def test_empty_substrate_still_produces_the_shape(tmp_path, clock):
    snap = snapshot.build_snapshot(Substrate(tmp_path / "e"), now=clock.t)
    assert set(snap) == set(EXAMPLE) and snap["score"]["best_cost"] is None and snap["current_direction"] is None
    assert snap["islands"] == [] and snap["decisions"] == [] and snap["history"] == []


def test_in_flight_jobs_expire(populated, clock):
    later = clock.t + timedelta(minutes=30)
    assert snapshot.build_snapshot(populated, now=later)["health"]["assistants"]["running"] == 0


def test_verifier_backlog(populated, clock):
    h.member(populated, "c")
    v = snapshot.build_snapshot(populated, now=clock.t + timedelta(minutes=10))["health"]["verifier"]
    assert v["backlog"] == 1 and v["oldest_wait_min"] == 10.0


def test_write_snapshot_is_atomic(tmp_path):
    p = tmp_path / "d" / "snapshot.json"
    snapshot.write_snapshot(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1} and not list(p.parent.glob("*.tmp"))
