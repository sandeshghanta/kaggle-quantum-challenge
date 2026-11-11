import pytest
from conftest import A, B, C, C1, C2, D, commit, measure

from substrate._projections import lineage, node


def test_up_returns_ancestors_nearest_first(st):
    commit(st, A, None)
    commit(st, B, A)
    commit(st, C, B)
    assert [n.program for n in lineage(st, C)] == [B, A]
    assert [n.program for n in lineage(st, "prog:" + C)] == [B, A]


def test_down_returns_all_descendants_once(st):
    commit(st, A, None)
    commit(st, B, A)
    commit(st, C, A)
    commit(st, D, B)
    assert [n.program for n in lineage(st, A, direction="down")] == [B, C, D]


def test_duplicate_commit_is_one_node(st):  # Review Focus 5
    commit(st, A, None)
    commit(st, B, A)
    commit(st, B, A)
    assert [n.program for n in lineage(st, A, direction="down")] == [B]
    assert node(st, B).seq == 2


def test_best_cost_from_passing_candidates(st):
    commit(st, A, None)
    measure(st, C1, 50, program=A)
    measure(st, C2, 40, program=A, passes=False)
    assert node(st, A).best_cost == 50 and node(st, "sha256:" + "f" * 64) is None


def test_up_stops_at_unknown_parent(st):
    commit(st, B, A)  # A was never committed
    assert lineage(st, B) == []


def test_bad_direction_rejected(st):
    with pytest.raises(ValueError):
        lineage(st, A, direction="sideways")
