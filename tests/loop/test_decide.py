import json

import pytest

from loop import decide, pool
from substrate.cli import Substrate
from tests.loop import helpers as h

ACTOR = {"role": "researcher"}
GOOD_PRED = {"metric": "island_best_cost", "island": "main", "threshold": 100000, "by_attempts": 300}


def doc(**kw):
    d = {"directions": [{"brief": "b", "why": "w", "prediction": dict(GOOD_PRED), "islands": ["main"],
                         "island_ops": [{"op": "update", "island": "main", "weight": 2.0}]}],
         "lessons": {"working": ["w"], "not_working": ["n"], "open_questions": []}, "questions": ["q?"]}
    return d | kw


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "s")
    h.island(s, "main")
    h.member(s, "a", cost=500.0)
    return s


def visit(sub):
    return sub.append("researcher.visited", {"trigger": "schedule", "digest": h.prog("d"), "model": "m"},
                      subject=h.BENCH, actor=ACTOR)


def test_parse_plain_and_fenced():
    plain = decide.parse_output(json.dumps(doc()))
    fenced = decide.parse_output("Here you go\n```json\n" + json.dumps(doc()) + "\n```\nbye")
    assert plain == fenced and plain.directions[0].island_ops[0]["weight"] == 2.0
    assert plain.lessons["working"] == ["w"] and plain.questions == ["q?"]


def test_parse_empty_decisions_are_valid():
    d = decide.parse_output('{"directions": [], "lessons": null, "questions": []}')
    assert d == decide.Decisions([], None, [])


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["directions"][0].pop("brief"), "brief"),
    (lambda d: d["directions"][0].pop("prediction"), "prediction"),
    (lambda d: d["directions"][0]["prediction"].update(threshold="low"), "threshold"),
    (lambda d: d["directions"][0]["prediction"].update(threshold=True), "threshold"),
    (lambda d: d["directions"][0]["prediction"].update(metric="island:3/best_cost"), "metric"),
    (lambda d: d["directions"][0]["prediction"].update(by_attempts=0), "by_attempts"),
    (lambda d: d["directions"][0]["prediction"].update(island=None), "island"),
    (lambda d: d["directions"][0]["island_ops"].append({"op": "explode"}), "unknown op"),
    (lambda d: d["directions"][0]["island_ops"].append({"op": "create", "island": "x"}), "idea"),
    (lambda d: d["directions"][0]["island_ops"].append({"op": "update", "island": "main"}), "changes nothing"),
    (lambda d: d["directions"][0]["island_ops"].append({"op": "update", "island": "main", "status": "dead"}), "status"),
    (lambda d: d["directions"][0]["island_ops"].append({"op": "restart", "island": "main", "program": "nope"}), "program"),
    (lambda d: d.update(questions="why"), "questions"),
    (lambda d: d.update(lessons=["x"]), "lessons"),
])
def test_parse_rejects_invalid(mutate, match):
    d = doc()
    mutate(d)
    with pytest.raises(decide.OutputError, match=match):
        decide.parse_output(json.dumps(d))


def test_parse_rejects_non_json():
    for bad in ("I think we should...", "{not json}", "[1, 2]"):
        with pytest.raises(decide.OutputError):
            decide.parse_output(bad)


def test_bench_metric_island_is_forced_null():
    d = doc()
    d["directions"][0]["prediction"] = {"metric": "bench_best_cost", "island": "main", "threshold": 1, "by_attempts": 1}
    assert decide.parse_output(json.dumps(d)).directions[0].prediction["island"] is None


def test_apply_writes_chain(sub):
    v = visit(sub)
    d = doc()
    d["directions"][0]["island_ops"] = [
        {"op": "create", "island": "new", "idea": "i", "instructions": "go", "weight": 0.5, "seed_from": h.prog("a")},
        {"op": "update", "island": "main", "weight": 2.0, "status": "retired"},
        {"op": "restart", "island": "new", "program": h.prog("a")},
        {"op": "migrate", "program": h.prog("a"), "to_island": "main"}]
    ids = decide.apply(sub, decide.parse_output(json.dumps(d)), visit_id=v, actor=ACTOR)
    evs = {e.id: e for e in sub.since(None)[0]}
    chain = [evs[i] for i in ids]
    assert [e.type for e in chain] == ["direction", "island.created", "island.joined", "island.updated",
                                       "island.joined", "island.joined", "lessons.published", "question.asked"]
    direction = chain[0]
    assert direction.cause == v and direction.subject == h.BENCH
    assert all(e.cause == direction.id for e in chain[1:6])
    assert chain[6].cause == v and chain[7].cause == v
    assert [e.payload["via"] for e in chain if e.type == "island.joined"] == ["restart", "restart", "migration"]
    assert chain[2].subject == "prog:" + h.prog("a")
    assert pool.islands(sub)["new"]["weight"] == 0.5 and pool.islands(sub)["main"]["status"] == "retired"
    assert pool.island_config_event(sub, "main") == chain[3].id


def test_apply_rejects_unknown_island_or_program_and_writes_nothing(sub):
    v = visit(sub)
    before = len(sub.since(None)[0])
    for op in ({"op": "update", "island": "ghost", "weight": 1}, {"op": "migrate", "program": h.prog("z"), "to_island": "main"},
               {"op": "migrate", "program": h.prog("a"), "to_island": "ghost"},
               {"op": "create", "island": "main", "idea": "i", "instructions": "x"}):
        d = doc()
        d["directions"][0]["island_ops"] = [op]
        ids, err = decide.parse_and_apply(sub, json.dumps(d), visit_id=v, actor=ACTOR)
        assert ids == [] and err
    assert len(sub.since(None)[0]) == before


def test_parse_and_apply_reports_parse_errors(sub):
    ids, err = decide.parse_and_apply(sub, "no json here", visit_id=visit(sub), actor=ACTOR)
    assert ids == [] and "no JSON" in err


def test_ops_may_reference_island_created_in_same_output(sub):
    d = doc()
    d["directions"] = [
        {"brief": "b", "why": "w", "prediction": GOOD_PRED, "island_ops": [
            {"op": "create", "island": "new", "idea": "i", "instructions": "x"}]},
        {"brief": "b2", "why": "w", "prediction": GOOD_PRED | {"island": "new"}, "island_ops": [
            {"op": "update", "island": "new", "weight": 3}]}]
    ids, err = decide.parse_and_apply(sub, json.dumps(d), visit_id=visit(sub), actor=ACTOR)
    assert err is None and len(ids) == 6
