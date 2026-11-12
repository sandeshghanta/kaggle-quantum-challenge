import random
from collections import Counter

import pytest

from loop import config, pool
from substrate.cli import Substrate

ACTOR = {"role": "test"}


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "s")
    s.append("island.created", {"island": "main", "idea": "i", "instructions": "x", "weight": 1.0},
             subject="bench:invsqrt", actor=ACTOR)
    return s


def member(sub, ch, cost=None, island="main", quick=None):
    prog = "sha256:" + ch * 64
    sub.append("program.committed", {"blob": prog, "parent": None, "benchmark": "invsqrt"},
               subject="prog:" + prog, actor=ACTOR)
    sub.append("island.joined", {"program": prog, "island": island, "via": "seed"},
               subject="prog:" + prog, actor=ACTOR)
    if cost is not None:
        sub.append("candidate.measured", {"program": prog, "circuit": "sha256:" + ch.upper().lower() * 63 + "0",
                                          "cost": cost, "passes": True, "benchmark": "invsqrt",
                                          "verifier": config.verifier_tag(), "cost_basis": "invsqrt/QxC/v1",
                                          "metrics": {"quick_cost": quick} if quick else {}},
                   subject="prog:" + prog, actor=ACTOR)
    return prog


def announce(sub, prog, n=1):
    for _ in range(n):
        sub.append("attempt.announced", {"island": "main", "plan": "pending"}, subject="prog:" + prog, actor=ACTOR)


def test_islands_fold_updates(sub):
    sub.append("island.updated", {"island": "main", "weight": 0.25}, subject="bench:invsqrt", actor=ACTOR)
    assert pool.islands(sub)["main"]["weight"] == 0.25 and pool.islands(sub)["main"]["status"] == "active"


def test_members_and_island_best(sub):
    a, b = member(sub, "a", 300.0), member(sub, "b", 100.0)
    member(sub, "c", 50.0, island="other")
    assert pool.members(sub, "main") == [a, b] and pool.island_best(sub, "main") == 100.0


def test_cheapest_parent_preferred_but_all_reachable(sub):
    progs = [member(sub, ch, cost) for ch, cost in [("a", 100.0), ("b", 200.0), ("c", 300.0)]]
    rng = random.Random(0)
    counts = Counter(pool.pick_parent(sub, "main", rng) for _ in range(1000))
    assert counts.most_common(1)[0][0] == progs[0] and all(counts[p] > 0 for p in progs)


def test_children_discount_spreads_picks(sub):
    progs = [member(sub, ch, cost) for ch, cost in [("a", 100.0), ("b", 200.0), ("c", 300.0)]]
    announce(sub, progs[0], 50)
    rng = random.Random(0)
    counts = Counter(pool.pick_parent(sub, "main", rng) for _ in range(1000))
    assert counts.most_common(1)[0][0] != progs[0]


def test_concurrent_picks_on_single_member(sub):  # Review Focus 5
    only = member(sub, "a")
    picks = [pool.pick_parent(sub, "main", random.Random(i)) for i in range(2)]
    for p in picks:
        announce(sub, p)
    assert picks == [only, only] and pool.children(sub, only) == 2


def test_empty_island_raises(sub):
    with pytest.raises(LookupError):
        pool.pick_parent(sub, "main", random.Random(0))


def test_pick_island_weighted_and_active_only(sub):
    member(sub, "a")
    sub.append("island.created", {"island": "dead", "idea": "i", "instructions": "x", "weight": 100.0},
               subject="bench:invsqrt", actor=ACTOR)
    sub.append("island.updated", {"island": "dead", "status": "retired"}, subject="bench:invsqrt", actor=ACTOR)
    assert {pool.pick_island(sub, random.Random(i)) for i in range(20)} == {"main"}


def test_island_best_quick_is_quick_cost_of_best_member(sub):
    member(sub, "a", 300.0, quick=290.0)
    member(sub, "b", 100.0, quick=99.0)
    assert pool.island_best_quick(sub, "main") == 99.0


def test_pick_island_skips_empty_islands(sub):
    member(sub, "a")
    sub.append("island.created", {"island": "empty", "idea": "i", "instructions": "x", "weight": 100.0},
               subject="bench:invsqrt", actor=ACTOR)
    assert {pool.pick_island(sub, random.Random(i)) for i in range(20)} == {"main"}


def test_outlier_cost_does_not_overflow(sub):  # review C1
    for ch, cost in zip("abcde", [2011100.0, 1900000.0, 1899000.0, 1898500.0, 1898400.0]):
        member(sub, ch, cost)
    rng = random.Random(0)
    assert all(pool.pick_parent(sub, "main", rng) for _ in range(50))


def test_rejected_programs_are_not_picked(sub):  # review I4
    good = member(sub, "a", 100.0)
    for ch in "bcdef9":
        bad = member(sub, ch)
        sub.append("candidate.rejected", {"program": bad, "reason": "duplicate"}, subject="prog:" + bad, actor=ACTOR)
    rng = random.Random(0)
    assert {pool.pick_parent(sub, "main", rng) for _ in range(200)} == {good}
