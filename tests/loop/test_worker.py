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


def test_announcement_cause_is_latest_island_config_event(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False))
    upd = sub.append("island.updated", {"island": "main", "weight": 2.0}, subject="bench:invsqrt",
                     actor={"role": "t"})
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    [ann] = only(sub, "attempt.announced")
    assert ann.cause == upd


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
