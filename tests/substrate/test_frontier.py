from conftest import C1, C2, C3, C4, P, measure

from substrate._projections import candidates, frontier


def test_worst_measurement_wins_and_counts(st):
    measure(st, C1, 100)
    measure(st, C1, 120)
    [c] = candidates(st, "invsqrt")
    assert c.cost == 120 and c.measurements == 2 and c.first_seq == 1 and c.last_seq == 2


def test_one_failing_measurement_fails_candidate(st):
    measure(st, C1, 100)
    measure(st, C1, 100, passes=False)
    assert candidates(st, "invsqrt")[0].passes is False and frontier(st, "invsqrt") == []


def test_only_latest_verifier_counts(st):
    measure(st, C1, 50, verifier="kickmix@1")
    measure(st, C1, 90, verifier="kickmix@2")
    [c] = candidates(st, "invsqrt")
    assert c.cost == 90 and c.measurements == 1


def test_frontier_never_mixes_verifiers_or_bases(st):  # Review Focus 4
    measure(st, C1, 10, verifier="kickmix@1")
    measure(st, C2, 20, verifier="kickmix@2")
    measure(st, C3, 5, verifier="kickmix@2", basis="invsqrt/QxC/v2")
    measure(st, C4, 30, verifier="kickmix@2")
    assert [c.circuit for c in frontier(st, "invsqrt")] == [C2, C4]
    assert [c.circuit for c in frontier(st, "invsqrt", verifier="kickmix@1")] == [C1]


def test_ties_go_to_earliest_and_k_limits(st):
    measure(st, C1, 10)
    measure(st, C2, 10)
    measure(st, C3, 5)
    assert [c.circuit for c in frontier(st, "invsqrt", k=2)] == [C3, C1]


def test_candidate_joins_program_commit(st):
    st.append("program.committed", {"blob": P, "parent": None, "benchmark": "invsqrt", "note": "n"},
              subject="prog:" + P, actor={}, cause=None)
    measure(st, C1, 10)
    c = candidates(st, "invsqrt")[0]
    assert c.note == "n" and c.parent is None


def test_empty_benchmark_has_empty_frontier(st):
    assert frontier(st, "invsqrt") == [] and candidates(st, "invsqrt") == []
