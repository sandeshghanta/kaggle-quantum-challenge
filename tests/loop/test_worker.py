import random

import pytest

from loop import worker
from loop.check import CheckReport
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


PASS = CheckReport(True, "ok", None, qubits=40, toffolis=100.0, cost_estimate=4000.0, samples=10)


@pytest.fixture(autouse=True)
def fast_check(monkeypatch, request):
    """The worker's final gate, faked (fast); tests marked real_check use the actual build + sample check."""
    if "real_check" not in request.keywords:
        monkeypatch.setattr(worker, "check_bundle", lambda bundle, **kw: PASS)


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
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False, ok=False, error="connection refused"))
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
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False, ok=False, error="timeout"))
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



# ── the gate after the job ─────────────────────────────────────────────
def gate_with(monkeypatch, report):
    monkeypatch.setattr(worker, "check_bundle", lambda bundle, **kw: report)


def test_failing_final_check_abandons_with_the_error(sub, cfg, tmp_path, monkeypatch):
    gate_with(monkeypatch, CheckReport(False, "check", "x=2097153: phase error too big", qubits=40, samples=10))
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True))
    assert worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w") == "abandoned"
    [ab] = only(sub, "attempt.abandoned")
    [ann] = only(sub, "attempt.announced")
    assert ab.cause == ann.id and ab.subject == ann.subject
    assert ab.payload["reason"] == "check_failed" and "x=2097153" in ab.payload["detail"]
    assert ab.payload["note"] == "tightened Newton step" and len(only(sub, "program.committed")) == 1


def test_build_failure_and_budget_stop_are_told_apart(sub, cfg, tmp_path, monkeypatch):
    gate_with(monkeypatch, CheckReport(False, "build", "SyntaxError: invalid syntax"))
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True))
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True, ok=False, error="timeout"))
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    assert [a.payload["reason"] for a in only(sub, "attempt.abandoned")] == ["build_failed", "out_of_budget"]


def test_valid_but_worse_estimate_is_not_submitted(sub, cfg, tmp_path, monkeypatch):
    from loop import config
    seed = only(sub, "program.committed")[0].payload["blob"]
    sub.append("candidate.measured", {"program": seed, "circuit": "sha256:" + "1" * 64, "cost": 2000000.0,
                                      "passes": True, "benchmark": "invsqrt", "metrics": {"sweep_cost": 2100000.0},
                                      "verifier": config.verifier_tag(), "cost_basis": config.COST_BASIS},
               subject="prog:" + seed, actor={"role": "verifier"})
    best = worker.pool.island_best_sweep(sub, "main")
    assert best == 2100000.0
    gate_with(monkeypatch, CheckReport(True, "ok", None, qubits=40, cost_estimate=best * 1.2, samples=10))
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True))
    assert worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w") == "abandoned"
    [ab] = only(sub, "attempt.abandoned")
    assert ab.payload["reason"] == "not_better" and ab.payload["estimate"] == best * 1.2


def test_timed_out_job_with_a_passing_edit_is_still_committed(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True, ok=False, error="timeout"))
    assert worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w") == "committed"


def test_job_gets_the_check_tool_budget_and_recent_failures(sub, cfg, tmp_path, monkeypatch):
    gate_with(monkeypatch, CheckReport(False, "check", "x=7: scratch qubit q(30) did not return to |0>", samples=1))
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True, final="SUMMARY: freed q30 early"))
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    seen = {}

    def run(workdir, agent_dir, **kw):
        seen.update(kw)
        kw["on_event"]({"type": "tool_execution_end", "toolName": "check", "result": {}, "isError": False})
        return fake_run(edit=False)(workdir, agent_dir)

    monkeypatch.setattr(worker, "run_pi", run)
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    assert "check" in seen["tools"] and seen["extensions"][0].name == "tools.ts"
    assert seen["extra_env"]["LOOP_CHECK_MAX"] == "8" and seen["max_output_tokens"] == 30000
    assert seen["timeout_s"] == 900
    assert "check_failed: freed q30 early -- x=7: scratch qubit q(30)" in seen["prompt"]


def test_recent_failures_lists_newest_first_and_only_this_island(sub, cfg, tmp_path):
    for i, isl in enumerate(["main", "other", "main"]):
        sub.append("attempt.abandoned", {"island": isl, "reason": "check_failed", "detail": f"err {i}",
                                         "note": f"try {i}"}, subject="prog:x", actor={"role": "t"})
    text = worker.recent_failures(sub, "main")
    assert text.splitlines() == ["- check_failed: try 2 -- err 2", "- check_failed: try 0 -- err 0"]
    assert worker.recent_failures(sub, "nowhere") == "(none)"


@pytest.mark.real_check
def test_real_gate_commits_a_valid_edit_of_the_reference(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True))
    assert worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w") == "committed"


# ── plans and what others are doing ────────────────────────────────────
def test_plan_tool_call_is_recorded_once_against_the_job(sub, cfg, tmp_path, monkeypatch):
    def run(workdir, agent_dir, **kw):
        for idea in ("narrow the low adder window", "something else"):
            kw["on_event"]({"type": "tool_execution_start", "toolName": "plan", "args": {"idea": idea}})
        return fake_run(edit=False)(workdir, agent_dir)

    monkeypatch.setattr(worker, "run_pi", run)
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    [ann] = only(sub, "attempt.announced")
    [plan] = only(sub, "attempt.planned")
    assert plan.cause == ann.id and plan.subject == ann.subject
    assert plan.payload == {"island": "main", "plan": "narrow the low adder window"}


def planned(sub, island, idea, cause):
    sub.append("attempt.planned", {"island": island, "plan": idea}, subject="prog:x", actor={"role": "t"},
               cause=cause)


def test_in_progress_hides_ended_other_island_own_and_stale_plans(sub):
    from datetime import UTC, datetime, timedelta
    a = sub.append("attempt.announced", {"island": "main", "plan": "p"}, subject="prog:x", actor={"role": "t"})
    b = sub.append("attempt.announced", {"island": "main", "plan": "p"}, subject="prog:x", actor={"role": "t"})
    c = sub.append("attempt.announced", {"island": "main", "plan": "p"}, subject="prog:x", actor={"role": "t"})
    planned(sub, "main", "running idea", a)
    planned(sub, "main", "ended idea", b)
    planned(sub, "other", "elsewhere", a)
    planned(sub, "main", "my own idea", c)
    sub.append("attempt.abandoned", {"island": "main", "reason": "check_failed"}, subject="prog:x",
               actor={"role": "t"}, cause=b)
    assert worker.in_progress(sub, "main", exclude=c) == "- running idea"
    later = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    assert worker.in_progress(sub, "main", now_ts=later, exclude=c) == "(none)"


def test_recent_successes_lists_passing_measurements_newest_first(sub, cfg, tmp_path, monkeypatch):
    from loop import config
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True, final="SUMMARY: freed q30 early"))
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    prog = only(sub, "program.committed")[-1].payload["blob"]
    sub.append("candidate.measured", {"program": prog, "circuit": "sha256:" + "2" * 64, "cost": 1900000.0,
                                      "passes": True, "benchmark": "invsqrt", "metrics": {},
                                      "verifier": config.verifier_tag(), "cost_basis": config.COST_BASIS},
               subject="prog:" + prog, actor={"role": "verifier"})
    assert worker.recent_successes(sub, "main") == "- freed q30 early -- cost 1,900,000"
    assert worker.recent_successes(sub, "other") == "(none)"


def test_prompt_carries_in_progress_and_successes(sub, cfg, tmp_path, monkeypatch):
    other = sub.append("attempt.announced", {"island": "main", "plan": "p"}, subject="prog:x", actor={"role": "t"})
    planned(sub, "main", "merge the two lookups", other)
    seen = {}

    def run(workdir, agent_dir, **kw):
        seen.update(kw)
        return fake_run(edit=False)(workdir, agent_dir)

    monkeypatch.setattr(worker, "run_pi", run)
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    assert "In progress right now on this island" in seen["prompt"] and "- merge the two lookups" in seen["prompt"]
    assert "Recently worked on this island" in seen["prompt"] and "plan" in seen["tools"]


@pytest.mark.real_check
def test_commit_carries_the_circuit_the_final_check_built(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=True))
    worker.run_job(sub, rng=random.Random(0), cfg=cfg, workroot=tmp_path / "w")
    c = only(sub, "program.committed")[-1].payload
    assert c["circuit"].startswith("sha256:") and len(sub.get_blob(c["circuit"])) > 1000


def test_once_mode_exit_codes(sub, cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "connect", lambda target: sub)
    args = ["--substrate", "x", "--id", "1", "--base-url", "http://h/v1", "--model", "m", "--api-key-env", "K",
            "--workroot", str(tmp_path / "w"), "--once"]
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False))
    assert worker.main(args) == 0
    monkeypatch.setattr(worker, "run_pi", fake_run(edit=False, ok=False, error="connection refused"))
    assert worker.main(args) == worker.EXIT_FAILED

    def nothing(*a, **k):
        raise LookupError("no islands yet")
    monkeypatch.setattr(worker, "run_job", nothing)
    assert worker.main(args) == worker.EXIT_WAITING
