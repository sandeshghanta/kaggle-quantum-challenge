import pytest

from loop import state, verifier, verify
from loop.programs import pack
from substrate.cli import Substrate

BROKEN = "raise RuntimeError('boom {n}')\n"


@pytest.fixture
def sub(tmp_path):
    return Substrate(tmp_path / "s")


def commit(sub, tmp_path, gen):
    d = tmp_path / f"b{abs(hash(gen))}"
    d.mkdir()
    (d / "gen.py").write_text(gen)
    blob = sub.put_blob(pack(d))
    sub.append("program.committed", {"blob": blob, "parent": None, "benchmark": "invsqrt"},
               subject="prog:" + blob, actor={"role": "test"})
    return blob


def run_once(sub, tmp_path):
    return verifier.run_once(sub, state_path=tmp_path / "cursor", workdir=tmp_path / "work",
                             salts=[1], max_workers=None)


def rejected(sub):
    return sub.since(None, types=["candidate.rejected"])[0]


def test_evaluates_each_program_once(tmp_path, sub):  # Review Focus 4
    commit(sub, tmp_path, BROKEN.format(n=1))
    commit(sub, tmp_path, BROKEN.format(n=2))
    assert run_once(sub, tmp_path) == 2 and len(rejected(sub)) == 2
    assert run_once(sub, tmp_path) == 0 and len(rejected(sub)) == 2


def test_cursor_saved_after_each_program(tmp_path, sub, monkeypatch):
    a = commit(sub, tmp_path, BROKEN.format(n=1))
    b = commit(sub, tmp_path, BROKEN.format(n=2))
    real, calls = verify.evaluate, []

    def flaky(s, program, **kw):
        calls.append(program)
        if len(calls) == 2:
            raise KeyboardInterrupt  # a kill; ordinary exceptions are now recorded as verify_error
        return real(s, program, **kw)

    monkeypatch.setattr(verifier, "evaluate", flaky)
    with pytest.raises(KeyboardInterrupt):
        run_once(sub, tmp_path)
    first_seq = sub.history("prog:" + a, types=["program.committed"])[0].seq
    assert state.load_cursor(tmp_path / "cursor") == first_seq
    monkeypatch.setattr(verifier, "evaluate", real)
    assert run_once(sub, tmp_path) == 1
    assert [e.payload["program"] for e in rejected(sub)] == [a, b]


def test_skips_program_already_judged(tmp_path, sub):
    a = commit(sub, tmp_path, BROKEN.format(n=1))
    sub.append("candidate.rejected", {"program": a, "reason": "duplicate"}, subject="prog:" + a, actor={})
    assert run_once(sub, tmp_path) == 0


def test_cursor_file_roundtrip(tmp_path):
    assert state.load_cursor(tmp_path / "c") == 0
    state.save_cursor(tmp_path / "c", 42)
    assert state.load_cursor(tmp_path / "c") == 42


def test_unexpected_error_is_recorded_and_skipped(tmp_path, sub, monkeypatch):  # review I3
    a = commit(sub, tmp_path, BROKEN.format(n=1))
    b = commit(sub, tmp_path, BROKEN.format(n=2))
    real = verify.evaluate

    def explode_on_a(s, program, **kw):
        if program == a:
            raise MemoryError("simulated")
        return real(s, program, **kw)

    monkeypatch.setattr(verifier, "evaluate", explode_on_a)
    assert run_once(sub, tmp_path) == 2
    reasons = {e.payload["program"]: e.payload["reason"] for e in rejected(sub)}
    assert reasons == {a: "verify_error", b: "build_failed"}
    assert run_once(sub, tmp_path) == 0


def test_interrupted_program_records_nothing_and_is_redone(tmp_path, sub, monkeypatch):  # review: drain between salts
    a = commit(sub, tmp_path, BROKEN.format(n=1))
    b = commit(sub, tmp_path, BROKEN.format(n=2))
    real = verify.evaluate

    def stop_on_b(s, program, **kw):
        if program == b:
            raise verify.Interrupted()
        return real(s, program, **kw)

    monkeypatch.setattr(verifier, "evaluate", stop_on_b)
    assert run_once(sub, tmp_path) == 1
    assert [e.payload["program"] for e in rejected(sub)] == [a]
    monkeypatch.setattr(verifier, "evaluate", real)
    assert run_once(sub, tmp_path) == 1
    assert [e.payload["program"] for e in rejected(sub)] == [a, b]


def test_stop_callable_reaches_evaluate(tmp_path, sub, monkeypatch):
    commit(sub, tmp_path, BROKEN.format(n=1))
    seen = {}
    monkeypatch.setattr(verifier, "evaluate", lambda s, program, **kw: seen.update(kw))
    verifier.run_once(sub, state_path=tmp_path / "cursor", workdir=tmp_path / "work", salts=[1],
                      max_workers=None, stop=lambda: False)
    assert callable(seen.get("stop"))
