import json
from datetime import timedelta
from pathlib import Path

import pytest

from loop import researcher_view as rv
from substrate.cli import Substrate
from tests.loop import helpers as h

EXAMPLE = json.loads(Path("dashboard/snapshot.example.json").read_text())
EXAMPLE_KEYS = ["generated_at", "current_direction", "islands", "decisions", "decision_summary", "beliefs", "questions", "researcher"]



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


def view(sub, now, **kw):
    return rv.build_researcher_view(sub, now=now, cap_pct=80, next_visit=now + timedelta(minutes=45), **kw)


def test_keys_and_shapes_match_example(populated, clock):
    v = view(populated, clock.t)
    assert sorted(v) == sorted(EXAMPLE_KEYS)
    for k in ("current_direction", "decision_summary", "beliefs"):
        assert set(v[k]) == set(EXAMPLE[k]), k
    for k in ("decisions", "questions"):
        assert set(v[k][0]) == set(EXAMPLE[k][0]), k
    assert set(v["islands"][0]) == set(EXAMPLE["islands"][0]) - {"assistants"}
    assert set(v["researcher"]) == {"five_hour_pct", "seven_day_pct", "cap_pct", "visits_24h", "next_visit", "last_visit"}
    assert set(v["researcher"]) - set(EXAMPLE["health"]["researcher"]) == {"last_visit"}
    for live in ("score", "history", "marks", "health", "example"):
        assert live not in v
    json.dumps(v, allow_nan=False)


def test_values(populated, clock):
    v = view(populated, clock.t)
    [dec] = v["decisions"]
    assert dec["id"] == "D1" and dec["outcome"] == "met" and dec["tokens"] == 31000
    assert "island main best cost below 1,500,000" in dec["prediction"]
    assert v["decision_summary"] == {"closed": 1, "paid_off": 1, "tokens_per_improvement": 31000}
    isl = {r["id"]: r for r in v["islands"]}
    assert isl["poly"]["call"] == "throttled" and isl["main"]["call"] == "new" and isl["poly"]["best_cost"] is None
    assert v["current_direction"]["progress"] == "2 / 10 attempts"
    assert v["current_direction"]["explore_share"] == round(0.2 / 1.2, 2)
    r = v["researcher"]
    assert r["five_hour_pct"] == 40.0 and r["cap_pct"] == 80 and r["visits_24h"] == 1
    assert r["next_visit"] == "2026-11-12T18:45:00Z" or r["next_visit"] == rv.iso(clock.t + timedelta(minutes=45))
    assert r["last_visit"].startswith("2026-11-12T18:00")
    assert v["beliefs"] == {"working": ["w"], "not_working": ["n"], "open_questions": ["o"]}
    assert [q["text"] for q in v["questions"]] == ["Q?"]


def test_empty_substrate_still_produces_the_shape(tmp_path, clock):
    v = rv.build_researcher_view(Substrate(tmp_path / "e"), now=clock.t, cap_pct=80, next_visit=None)
    assert sorted(v) == sorted(EXAMPLE_KEYS) and v["current_direction"] is None and v["islands"] == []
    assert v["researcher"]["next_visit"] is None and v["researcher"]["last_visit"] is None


def test_write_view_is_atomic(tmp_path):
    p = tmp_path / "d" / "researcher.json"
    rv.write_view(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1} and not list(p.parent.glob("*.tmp"))
