import json
from pathlib import Path

import pytest

from substrate import _schemas as s

FIX = Path("substrate/fixtures")


def test_every_type_has_a_fixture_and_vice_versa():
    assert sorted(p.name for p in FIX.iterdir()) == s.event_types()
    assert len(s.event_types()) == 15 and "envelope" not in s.event_types()


@pytest.mark.parametrize("t", s.event_types())
def test_fixture_validates(t):
    s.validate_payload(t, 1, json.loads((FIX / t / "v1.json").read_text()))


def test_missing_required_field_is_rejected():
    with pytest.raises(s.SchemaViolation, match="direction"):
        s.validate_payload("direction", 1, {"brief": "x", "why": "y"})


def test_unknown_type_is_rejected():
    with pytest.raises(s.SchemaViolation):
        s.latest_version("no.such.type")


def test_unknown_version_raises():
    with pytest.raises(s.UnknownVersion):
        s.validate_payload("direction", 99, {})


def test_extra_payload_fields_are_allowed():  # tolerant reader
    p = json.loads((FIX / "question.asked" / "v1.json").read_text()) | {"new_field": 1}
    s.validate_payload("question.asked", 1, p)


def test_envelope_rejects_unknown_top_level_field():
    env = {"v": 1, "id": "01JC7KQF8R2M4VX1A0BZPQ3XYZ", "ts": "2026-11-11T19:40:12.418Z",
           "type": "question.asked", "type_v": 1, "subject": "bench:invsqrt",
           "actor": {}, "payload": {"question": "q"}, "extra": 1}
    with pytest.raises(s.SchemaViolation):
        s.validate_envelope(env)
