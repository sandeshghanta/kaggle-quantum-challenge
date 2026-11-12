import json
from datetime import timedelta

import pytest

from loop import digest
from substrate.cli import Substrate
from tests.loop import helpers as h


@pytest.fixture
def clock(monkeypatch):
    return h.Clock(monkeypatch)


@pytest.fixture
def sub(tmp_path, clock):
    s = Substrate(tmp_path / "s")
    h.island(s, "main", idea="reference", instructions="x" * 2000)
    return s


def test_islands_rows(sub, clock):
    h.member(sub, "a", cost=200.0)
    clock.advance(hours=30)
    h.member(sub, "b", cost=150.0)
    h.attempt(sub, n=3)
    d = digest.build_digest(sub, now=clock.t)
    [row] = d["islands"]
    assert row["id"] == "main" and row["idea"] == "reference" and row["status"] == "active"
    assert row["members"] == 2 and row["best_cost"] == 150.0 and row["best_sweep_cost"] == 150.0
    assert row["attempts_24h"] == 3
    assert row["trend_24h_pct"] == -25.0
    assert len(row["instructions"]) == digest.INSTRUCTIONS_CHARS


def test_trend_is_null_without_old_measurement(sub, clock):
    h.member(sub, "a", cost=200.0)
    d = digest.build_digest(sub, now=clock.t)
    assert d["islands"][0]["trend_24h_pct"] is None


def test_frontier_top5(sub, clock):
    for i, ch in enumerate("abcdefg"):
        h.member(sub, ch, cost=100.0 + i, Q=80 + i, T=5.0)
    f = digest.build_digest(sub, now=clock.t)["frontier"]
    assert [c["cost"] for c in f] == [100.0, 101.0, 102.0, 103.0, 104.0]
    assert f[0] == {"cost": 100.0, "Q": 80, "T": 5.0, "program": h.prog("a"), "island": "main"}


def test_prediction_met_missed_open(sub, clock):
    h.member(sub, "a", cost=200.0)
    met = h.direction(sub, threshold=150.0, by=5)
    missed = h.direction(sub, threshold=10.0, by=3)
    open_ = h.direction(sub, threshold=10.0, by=50)
    h.attempt(sub, n=2)
    h.member(sub, "b", cost=140.0)
    h.attempt(sub, n=2)
    ds = {x["id"]: x for x in digest.build_digest(sub, now=clock.t)["directions"]}
    assert ds[met]["status"] == "met" and ds[missed]["status"] == "missed" and ds[open_]["status"] == "open"
    assert ds[open_]["attempts_used"] == 4 and ds[open_]["best_since"] == 140.0


def test_prediction_met_too_late_is_missed(sub, clock):
    h.member(sub, "a", cost=200.0)
    d = h.direction(sub, threshold=150.0, by=2)
    h.attempt(sub, n=4)
    h.member(sub, "b", cost=140.0)
    [row] = digest.build_digest(sub, now=clock.t)["directions"]
    assert row["id"] == d and row["status"] == "missed"


def test_prediction_scope_island_vs_bench(sub, clock):
    h.island(sub, "other")
    h.member(sub, "a", island="main", cost=200.0)
    isl = h.direction(sub, island="other", threshold=150.0, by=5)
    bench = h.direction(sub, island="main", metric="bench_best_cost", threshold=150.0, by=5)
    h.member(sub, "b", island="main", cost=100.0)
    ds = {x["id"]: x for x in digest.build_digest(sub, now=clock.t)["directions"]}
    assert ds[isl]["status"] == "open" and ds[bench]["status"] == "met"


def test_measurements_before_direction_do_not_count(sub, clock):
    h.member(sub, "a", cost=100.0)
    h.direction(sub, threshold=150.0, by=5)
    [row] = digest.build_digest(sub, now=clock.t)["directions"]
    assert row["status"] == "open" and row["best_since"] is None


def test_rejections_tally_with_samples(sub, clock):
    for i in range(5):
        p = h.member(sub, "abcde"[i])
        sub.append("candidate.rejected", {"program": p, "reason": "build_failed", "detail": f"boom {i}"},
                   subject="prog:" + p, actor={"role": "verifier"})
    p = h.member(sub, "f")
    sub.append("candidate.rejected", {"program": p, "reason": "duplicate"}, subject="prog:" + p,
               actor={"role": "verifier"})
    r = digest.build_digest(sub, now=clock.t)["rejections_24h"]
    assert r["by_reason"] == {"build_failed": 5, "duplicate": 1}
    assert [s["detail"] for s in r["samples"]] == ["boom 4", "boom 3", "boom 2"]
    old = digest.build_digest(sub, now=clock.t + timedelta(days=2))["rejections_24h"]
    assert old == {"by_reason": {}, "samples": []}


def test_hit_rate(sub, clock):
    h.member(sub, "a", cost=100.0)
    h.member(sub, "b")
    h.member(sub, "c", actor={"role": "seed"}, cost=90.0)  # not a lab assistant's
    h.attempt(sub, n=4)
    assert digest.build_digest(sub, now=clock.t)["lab_assistants_24h"] == {
        "attempts": 4, "committed": 2, "passing": 1, "hit_rate": 0.25}


def test_lessons_questions_and_instructions(sub, clock):
    d0 = digest.build_digest(sub, now=clock.t)
    assert d0["lessons"] is None and d0["human_instructions"] == [] and d0["last_visit"] is None
    sub.append("lessons.published", {"working": ["w"], "not_working": ["n"]}, subject=h.BENCH, actor=h.RES)
    sub.append("lessons.published", {"working": ["w2"], "not_working": []}, subject=h.BENCH, actor=h.RES)
    sub.append("question.asked", {"question": "why?"}, subject=h.BENCH, actor=h.RES)
    old = sub.append("human.instructed", {"text": "old", "kind": "idea"}, subject=h.BENCH, actor={"role": "human"})
    sub.append("researcher.visited", {"trigger": "human", "digest": h.prog("c"), "model": "m"},
               subject=h.BENCH, actor=h.RES, cause=old)
    new = sub.append("human.instructed", {"text": "focus on Q", "kind": "steer"}, subject=h.BENCH,
                     actor={"role": "human"})
    d = digest.build_digest(sub, now=clock.t)
    assert d["lessons"]["working"] == ["w2"]
    assert [q["question"] for q in d["open_questions"]] == ["why?"]
    assert [i["id"] for i in d["human_instructions"]] == [new]
    assert digest.unanswered_instructions(sub)[0].id == new
    assert digest.build_digest(sub, now=clock.t + timedelta(days=8))["open_questions"] == []


def test_budget(sub, clock):
    sub.append("quota.observed", {"five_hour_pct": 40.0, "seven_day_pct": 22.0}, subject="pool:claude", actor=h.RES)
    sub.append("budget.spent", {"pool": "claude", "tokens_in": 1000, "tokens_out": 200}, subject="pool:claude",
               actor=h.RES)
    sub.append("budget.spent", {"pool": "qwen", "tokens_in": 5, "tokens_out": 6}, subject="pool:qwen", actor=h.LAB)
    sub.append("budget.spent", {"pool": "cpu", "tokens_in": 0, "tokens_out": 0, "cpu_seconds": 12.34},
               subject="pool:cpu", actor=h.LAB)
    b = digest.build_digest(sub, now=clock.t)["budget"]
    assert b["quota"]["five_hour_pct"] == 40.0
    assert b["spend_24h"]["claude"]["tokens_in"] == 1000 and b["spend_24h"]["qwen"]["tokens_out"] == 6
    assert b["spend_24h"]["cpu"]["cpu_seconds"] == 12.3
    assert digest.build_digest(sub, now=clock.t + timedelta(days=2))["budget"]["spend_24h"]["claude"]["events"] == 0


def test_digest_is_json_and_compact(sub, clock):
    for i in range(8):
        h.island(sub, f"isl{i}", idea="a fairly long idea " * 3, instructions="detailed instructions " * 60)
        for j in range(3):
            h.member(sub, f"m{i}-{j}", island=f"isl{i}", cost=1000.0 - i * 10 - j)
    for k in range(30):
        h.direction(sub, island=f"isl{k % 8}", threshold=1.0, by=1000)
    for k in range(40):
        sub.append("candidate.rejected", {"program": h.prog("a"), "reason": "build_failed",
                                          "detail": "Traceback " * 100}, subject="prog:" + h.prog("a"),
                   actor={"role": "verifier"})
    text = json.dumps(digest.build_digest(sub, now=clock.t))
    assert len(text) / 4 < 6000, len(text) / 4
