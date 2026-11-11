import json
import sqlite3

import pytest

from substrate import upcasters as u
from substrate._schemas import UnknownVersion, event_types, latest_version, validate_payload
from substrate._store import Store


def test_upcast_is_identity_at_latest():
    assert u.upcast("question.asked", 1, {"question": "q"}) == (1, {"question": "q"})


def test_upcast_chains_steps(monkeypatch):
    monkeypatch.setattr(u, "latest_version", lambda t: 3)
    reg = {("t", 1): lambda p: p | {"b": 1}, ("t", 2): lambda p: p | {"c": 1}}
    assert u.upcast("t", 1, {"a": 1}, reg) == (3, {"a": 1, "b": 1, "c": 1})


def test_missing_step_refused(monkeypatch):
    monkeypatch.setattr(u, "latest_version", lambda t: 2)
    with pytest.raises(UnknownVersion):
        u.upcast("t", 1, {}, {})


def test_future_type_version_refused():  # Review Focus 3
    with pytest.raises(UnknownVersion):
        u.upcast("question.asked", 2, {})


def test_future_envelope_version_refused_on_read(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    st.append("question.asked", {"question": "q"}, subject="bench:x", actor={})
    db = sqlite3.connect(tmp_path / "e.sqlite")
    db.execute("INSERT INTO events (v,id,ts,type,type_v,subject,actor,cause,payload) "
               "VALUES (2,'01JC7KQF8R2M4VX1A0BZPQ3XYZ','2026-11-11T00:00:00.000Z',"
               "'question.asked',1,'bench:x','{}',NULL,'{}')")
    db.commit()
    with pytest.raises(UnknownVersion):
        st.history("bench:x")


@pytest.mark.parametrize("t", event_types())
def test_every_fixture_upcasts_to_a_valid_latest_payload(t):
    p = json.loads(open(f"substrate/fixtures/{t}/v1.json").read())
    v, out = u.upcast(t, 1, p)
    validate_payload(t, v, out)
    assert v == latest_version(t)
