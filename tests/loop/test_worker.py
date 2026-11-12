import random

import pytest

from loop import worker
from loop.pi_runner import PiRun
from loop.seed import seed_reference
from substrate.cli import Substrate


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "s")
    seed_reference(s)
    return s


@pytest.fixture
def cfg(tmp_path):
    return worker.WorkerConfig(base_url="http://127.0.0.1:1/v1", model="fake-model", api_key_env="FAKE_KEY",
                               context_window=32768, max_tokens=4096, role_dir=worker.DEFAULT_ROLE_DIR,
                               worker_id=1)


def fake_run(edit: bool, ok: bool = True, error: str | None = None, final="SUMMARY: tightened Newton step"):
    def run(workdir, agent_dir, **kw):
        if edit:
            p = workdir / "gen.py"
            p.write_text(p.read_text() + "\n# tweak\n")
        return PiRun(ok=ok, exit_code=0 if ok else None, final_text=final, tokens_in=500, tokens_out=60,
                     tokens_cached=400, seconds=1.5, error=error)
    return run


def only(sub, t):
    return sub.since(None, types=[t])[0]


def test_committed_job_records_full_chain(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True))
    seed = only(sub, "program.committed")[0].payload["blob"]
    assert worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w") == "committed"
    [ann] = only(sub, "attempt.announced")
    [spent] = only(sub, "budget.spent")
    commits = only(sub, "program.committed")
    assert len(commits) == 2 and commits[1].payload["parent"] == seed and commits[1].cause == ann.id
    assert commits[1].payload["note"] == "tightened Newton step"
    assert spent.cause == ann.id and spent.payload["pool"] == "qwen" and spent.payload["tokens_out"] == 60
    joined = sub.history("prog:" + commits[1].payload["blob"], types=["island.joined"])
    assert joined[0].payload["via"] == "submit"
    assert not any((tmp_path / "w").iterdir())


def test_unchanged_job_commits_nothing(sub, cfg, tmp_path, monkeypatch):  # Review Focus 3
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False))
    assert worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w") == "unchanged"
    assert len(only(sub, "program.committed")) == 1 and len(only(sub, "budget.spent")) == 1


def test_failed_run_still_records_spend(sub, cfg, tmp_path, monkeypatch):  # Review Focus 3
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True, ok=False, error="timeout"))
    assert worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w") == "failed"
    assert len(only(sub, "program.committed")) == 1 and len(only(sub, "budget.spent")) == 1


def test_interrupted_job_leaves_only_orphan_announcement(sub, cfg, tmp_path, monkeypatch):  # Review Focus 2
    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(worker, "run_pi", boom)
    with pytest.raises(KeyboardInterrupt):
        worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    assert len(only(sub, "attempt.announced")) == 1 and len(only(sub, "program.committed")) == 1
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True))
    assert worker.run_job(sub, rng=random.Random(1), cfg=cfg, workroot=tmp_path / "w") == "committed"


def test_backoff_grows_and_caps():  # review I5
    assert worker.backoff_seconds(0) == 0
    assert worker.backoff_seconds(1) < worker.backoff_seconds(3) <= worker.backoff_seconds(20) == 300


def beat(tmp_path, n=1):
    import json
    return json.loads((tmp_path / "st" / f"worker-{n}.json").read_text())


def test_plan_is_announced_not_pending(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False))
    seed = only(sub, "program.committed")[0].payload["blob"]
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    [ann] = only(sub, "attempt.announced")
    assert ann.payload["plan"] == f"main: improving parent {seed.removeprefix('sha256:')[:12]}"


def test_heartbeats_follow_the_job(sub, cfg, tmp_path, monkeypatch):
    import dataclasses
    cfg = dataclasses.replace(cfg, status_dir=tmp_path / "st")
    seen = {}

    def run(workdir, agent_dir, **kw):
        seen["start"] = beat(tmp_path)
        kw["on_event"]({"type": "tool_execution_start", "toolName": "edit", "args": {"path": "gen.py"}})
        kw["on_event"]({"type": "message_end", "message": {"role": "assistant", "usage": {"output": 7}}})
        seen["mid"] = beat(tmp_path)
        return fake_run(edit=True)(workdir, agent_dir)

    monkeypatch.setattr(worker, "run_pi", run)
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    [ann] = only(sub, "attempt.announced")
    assert seen["start"]["state"] == "thinking" and seen["start"]["island"] == "main"
    assert seen["start"]["announcement"] == ann.id and seen["start"]["started_at"]
    assert seen["mid"]["turns"] == 1 and seen["mid"]["last_tool"] == "edit"
    assert seen["mid"]["files_edited"] == ["gen.py"] and seen["mid"]["tokens_out"] == 7
    end = beat(tmp_path)
    assert end["state"] == "idle" and end["last_outcome"] == "committed"
    assert end["last_summary"] == "tightened Newton step" and end["finished_at"]


def test_failed_job_heartbeat_says_failed(sub, cfg, tmp_path, monkeypatch):
    import dataclasses
    cfg = dataclasses.replace(cfg, status_dir=tmp_path / "st")
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True, ok=False, error="timeout"))
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    assert beat(tmp_path)["last_outcome"] == "failed" and beat(tmp_path)["last_summary"] == "timeout"


def test_crash_leaves_idle_failed_heartbeat(sub, cfg, tmp_path, monkeypatch):
    import dataclasses
    cfg = dataclasses.replace(cfg, status_dir=tmp_path / "st")

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(worker, "run_pi", boom)
    with pytest.raises(RuntimeError):
        worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    assert beat(tmp_path)["state"] == "idle" and beat(tmp_path)["last_outcome"] == "failed"


def test_no_status_dir_writes_nothing(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False))
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    assert not (tmp_path / "st").exists()
