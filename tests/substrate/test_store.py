import multiprocessing as mp
import sqlite3

import pytest

from substrate._schemas import SchemaViolation
from substrate._store import Store

A = {"role": "test"}
Q = {"question": "q"}


def test_append_returns_ulid_and_history_returns_it(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    eid = st.append("question.asked", Q, subject="bench:invsqrt", actor=A)
    [ev] = st.history("bench:invsqrt")
    assert ev.id == eid and len(eid) == 26 and ev.seq == 1 and ev.v == 1 and ev.type_v == 1
    assert ev.ts.endswith("Z") and ev.payload == Q and ev.cause is None


def test_invalid_payload_writes_nothing(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    with pytest.raises(SchemaViolation):
        st.append("question.asked", {}, subject="bench:invsqrt", actor=A)
    assert st.since(None) == ([], 0)


def test_bad_subject_shape_rejected(tmp_path):
    with pytest.raises(SchemaViolation):
        Store(tmp_path / "e.sqlite").append("question.asked", Q, subject="invsqrt", actor=A)


def test_history_filters_types_and_keeps_commit_order(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    st.append("question.asked", Q, subject="bench:x", actor=A)
    st.append("human.instructed", {"text": "t", "kind": "idea"}, subject="bench:x", actor=A)
    st.append("question.asked", Q, subject="bench:y", actor=A)
    assert [e.type for e in st.history("bench:x")] == ["question.asked", "human.instructed"]
    assert [e.type for e in st.history("bench:x", types=["human.instructed"])] == ["human.instructed"]


def test_since_pages_and_never_skips(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    for _ in range(5):
        st.append("question.asked", Q, subject="bench:x", actor=A)
    page, c = st.since(None, limit=3)
    assert [e.seq for e in page] == [1, 2, 3] and c == 3
    st.append("question.asked", Q, subject="bench:x", actor=A)
    page, c = st.since(c)
    assert [e.seq for e in page] == [4, 5, 6] and c == 6
    assert st.since(c) == ([], 6)


def test_update_and_delete_are_blocked(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    st.append("question.asked", Q, subject="bench:x", actor=A)
    db = sqlite3.connect(tmp_path / "e.sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE events SET subject='bench:z'")
        db.commit()
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM events")
        db.commit()


def _writer(path, n):
    st = Store(path)
    for _ in range(n):
        st.append("question.asked", Q, subject="bench:x", actor=A)


def test_concurrent_appends_from_processes(tmp_path):  # Review Focus 1
    path = tmp_path / "e.sqlite"
    Store(path)
    procs = [mp.get_context("spawn").Process(target=_writer, args=(path, 50)) for _ in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
        assert p.exitcode == 0
    evs, _ = Store(path).since(None, limit=10_000)
    assert [e.seq for e in evs] == list(range(1, 401)) and len({e.id for e in evs}) == 400
