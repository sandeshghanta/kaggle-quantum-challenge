import pytest

from substrate.cli import Forbidden, Substrate

Q = {"question": "q"}


def deny_questions(principal, type, subject, payload):
    if principal == "worker" and type in ("question.asked", "blob"):
        raise Forbidden(f"{principal} may not write {type}")


def test_hook_blocks_append_and_writes_nothing(tmp_path):
    s = Substrate(tmp_path, authorize=deny_questions)
    with pytest.raises(Forbidden):
        s.append("question.asked", Q, subject="bench:x", actor={}, principal="worker")
    assert s.since(None) == ([], 0)


def test_hook_blocks_put_blob(tmp_path):
    with pytest.raises(Forbidden):
        Substrate(tmp_path, authorize=deny_questions).put_blob(b"x", principal="worker")


def test_hook_allows_other_principals_and_in_process_use(tmp_path):
    s = Substrate(tmp_path, authorize=deny_questions)
    s.append("question.asked", Q, subject="bench:x", actor={}, principal="researcher")
    s.append("question.asked", Q, subject="bench:x", actor={})
    assert len(s.history("bench:x")) == 2


def test_no_hook_means_no_checks(tmp_path):
    Substrate(tmp_path).append("question.asked", Q, subject="bench:x", actor={}, principal="anyone")
