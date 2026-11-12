import json
import shutil
from datetime import timedelta

import pytest

from loop import decide, digest, pool, researcher
from loop.pi_runner import PiRun, write_agent_dir
from substrate.cli import Substrate
from tests.fakes.openai_server import FakeOpenAI
from tests.loop import helpers as h

PRED = {"metric": "island_best_cost", "island": "main", "threshold": 100.0, "by_attempts": 50}


def decision_json(**kw):
    d = {"directions": [{"brief": "double down", "why": "it is falling", "prediction": PRED, "islands": ["main"],
                         "island_ops": [{"op": "update", "island": "main", "weight": 3.0}]}],
         "lessons": {"working": ["w"], "not_working": ["n"], "open_questions": []}, "questions": []}
    return d | kw


@pytest.fixture
def clock(monkeypatch):
    return h.Clock(monkeypatch)


@pytest.fixture
def sub(tmp_path, clock):
    s = Substrate(tmp_path / "s")
    h.island(s, "main")
    h.member(s, "a", cost=500.0)
    return s


@pytest.fixture
def cfg(tmp_path):
    return researcher.ResearcherConfig(model="claude-opus-5-5", provider="claude-bridge", agent_dir=tmp_path / "agent",
                                       snapshot_path=tmp_path / "out" / "snapshot.json")


def fake_pi(text, *, ok=True, events=(), seen=None):
    def run(workdir, agent_dir, **kw):
        if seen is not None:
            seen.update(kw)
        for ev in events:
            kw["on_event"](ev)
        return PiRun(ok=ok, exit_code=0 if ok else 1, final_text=text, tokens_in=9000, tokens_out=700,
                     tokens_cached=8000, seconds=12.5, error=None if ok else "boom")
    return run


def of(sub, t):
    return sub.since(None, types=[t])[0]


# ── gate ───────────────────────────────────────────────────────────────
def observe(sub, five, seven, five_reset=None, seven_reset=None):
    sub.append("quota.observed", {"five_hour_pct": five, "seven_day_pct": seven, "five_hour_resets_at": five_reset,
                                  "seven_day_resets_at": seven_reset}, subject="pool:claude", actor=h.RES)


def test_gate_allows_without_observation(sub, clock):
    assert researcher.gate(sub, now=clock.t)[0]


@pytest.mark.parametrize("five,seven,ok", [(40, 22, True), (79.9, 79.9, True), (80, 10, False), (10, 80, False),
                                           (95, 95, False)])
def test_gate_thresholds(sub, clock, five, seven, ok):
    observe(sub, five, seven)
    allowed, reason = researcher.gate(sub, now=clock.t)
    assert allowed is ok and reason


def test_gate_cap_is_configurable(sub, clock):
    observe(sub, 85, 10)
    assert researcher.gate(sub, now=clock.t, cap_pct=90)[0]


def test_gate_treats_a_reset_window_as_fresh(sub, clock):
    past, future = "2026-11-11T11:00:00Z", "2026-11-11T20:00:00Z"
    observe(sub, 95, 10, five_reset=past)
    assert researcher.gate(sub, now=clock.t)[0]
    observe(sub, 95, 10, five_reset=future)
    assert not researcher.gate(sub, now=clock.t)[0]
    observe(sub, 10, 99, seven_reset=past)
    assert researcher.gate(sub, now=clock.t)[0]


def test_gate_uses_the_latest_observation(sub, clock):
    observe(sub, 95, 95)
    observe(sub, 10, 10)
    assert researcher.gate(sub, now=clock.t)[0]


# ── quota extraction ───────────────────────────────────────────────────
def test_quota_scan_headers():
    s = researcher.QuotaScan()
    s.feed({"type": "message_end", "message": {"headers": {
        "anthropic-ratelimit-unified-5h-utilization": "0.42", "anthropic-ratelimit-unified-5h-reset": "1794000000",
        "Anthropic_RateLimit_Unified_7d_Utilization": 0.1, "anthropic-ratelimit-unified-7d-reset": "1794400000"}}})
    q = s.result()
    assert q["five_hour_pct"] == 42.0 and q["seven_day_pct"] == 10.0
    assert q["five_hour_resets_at"].endswith("Z") and q["seven_day_resets_at"].endswith("Z")


def test_quota_scan_sdk_rate_limit_objects():
    s = researcher.QuotaScan()
    s.feed({"rate_limit": {"rateLimitType": "five_hour", "utilization": 0.5, "resetsAt": 1794000000}})
    assert s.result() is None
    s.feed({"x": [{"rate_limit_info": {"rateLimitType": "seven_day", "utilization": 33, "resetsAt": 1794400000}}]})
    q = s.result()
    assert q["five_hour_pct"] == 50.0 and q["seven_day_pct"] == 33.0


def test_quota_scan_finds_nothing_in_ordinary_events():
    s = researcher.QuotaScan()
    s.feed({"type": "message_end", "message": {"usage": {"input": 5}}})
    assert s.result() is None


# ── visit ──────────────────────────────────────────────────────────────
def test_visit_records_the_full_chain(sub, cfg, clock, monkeypatch):
    seen = {}
    quota_ev = {"headers": {"anthropic-ratelimit-unified-5h-utilization": 0.4,
                            "anthropic-ratelimit-unified-7d-utilization": 0.2}}
    monkeypatch.setattr(researcher, "run_pi", fake_pi("```json\n" + json.dumps(decision_json()) + "\n```",
                                                      events=[quota_ev], seen=seen))
    res = researcher.run_visit(sub, cfg, trigger="schedule", now=clock.t)
    assert res.error is None
    [visit] = of(sub, "researcher.visited")
    assert res.visit_id == visit.id and visit.payload["trigger"] == "schedule" and visit.payload["model"] == cfg.model
    shown = json.loads(sub.get_blob(visit.payload["digest"]))
    assert shown["islands"][0]["id"] == "main" and shown["now"] == "2026-11-11T12:00:00Z"
    [direction] = of(sub, "direction")
    assert direction.cause == visit.id
    [upd] = of(sub, "island.updated")
    assert upd.cause == direction.id and pool.island_config_event(sub, "main") == upd.id
    [lessons] = of(sub, "lessons.published")
    assert lessons.cause == visit.id
    [spent] = of(sub, "budget.spent")
    assert spent.payload["pool"] == "claude" and spent.cause == visit.id and spent.payload["tokens_out"] == 700
    [quota] = of(sub, "quota.observed")
    assert quota.payload["five_hour_pct"] == 40.0 and res.quota_event == quota.id
    assert seen["tools"] == [] and seen["provider"] == "claude-bridge" and seen["sandboxed"] is False
    assert '"id": "main"' in seen["prompt"] and "{digest}" not in seen["prompt"]
    snap = json.loads(cfg.snapshot_path.read_text())
    assert snap["example"] is False and snap["marks"][0]["label"] == "D1"
    assert digest.unanswered_instructions(sub) == []


def test_visit_cause_links_to_human_instruction(sub, cfg, clock, monkeypatch):
    monkeypatch.setattr(researcher, "run_pi", fake_pi(json.dumps(decision_json(directions=[], lessons=None))))
    inst = sub.append("human.instructed", {"text": "try Q<80", "kind": "steer"}, subject=h.BENCH,
                      actor={"role": "human"})
    researcher.run_visit(sub, cfg, trigger="human", cause=inst, now=clock.t)
    assert of(sub, "researcher.visited")[0].cause == inst and of(sub, "direction") == []
    assert digest.unanswered_instructions(sub) == []


def test_invalid_output_applies_nothing_but_records_spend(sub, cfg, clock, monkeypatch):
    monkeypatch.setattr(researcher, "run_pi", fake_pi("I would try something."))
    res = researcher.run_visit(sub, cfg, trigger="schedule", now=clock.t)
    assert res.error and res.event_ids == []
    assert of(sub, "direction") == [] and len(of(sub, "budget.spent")) == 1 and len(of(sub, "researcher.visited")) == 1


def test_failed_pi_run(sub, cfg, clock, monkeypatch):
    monkeypatch.setattr(researcher, "run_pi", fake_pi("", ok=False))
    res = researcher.run_visit(sub, cfg, trigger="schedule", now=clock.t)
    assert "pi failed" in res.error and of(sub, "direction") == []
    assert cfg.snapshot_path.exists()


# ── service loop ───────────────────────────────────────────────────────
def count_visits(monkeypatch):
    calls = []

    def run_visit(sub, cfg, *, trigger, cause=None, now):
        calls.append((trigger, cause))
        vid = sub.append("researcher.visited", {"trigger": trigger, "digest": h.prog("d"), "model": "m"},
                         subject=h.BENCH, actor=h.RES, cause=cause)
        return researcher.VisitResult(vid, [], None, None)
    monkeypatch.setattr(researcher, "run_visit", run_visit)
    return calls


def test_first_tick_visits_then_waits_for_the_interval(sub, cfg, clock, monkeypatch):
    calls = count_visits(monkeypatch)
    sched = researcher.initial_schedule(sub, cfg, now=clock.t)
    assert researcher.tick(sub, cfg, sched, now=clock.t) == "schedule"
    clock.advance(minutes=44)
    assert researcher.tick(sub, cfg, sched, now=clock.t) is None
    clock.advance(minutes=2)
    assert researcher.tick(sub, cfg, sched, now=clock.t) == "schedule" and len(calls) == 2


def test_restart_respects_the_last_visit(sub, cfg, clock, monkeypatch):
    calls = count_visits(monkeypatch)
    sub.append("researcher.visited", {"trigger": "schedule", "digest": h.prog("d"), "model": "m"},
               subject=h.BENCH, actor=h.RES)
    clock.advance(minutes=10)
    sched = researcher.initial_schedule(sub, cfg, now=clock.t)
    assert researcher.tick(sub, cfg, sched, now=clock.t) is None and calls == []


def test_human_instruction_triggers_an_immediate_visit_once(sub, cfg, clock, monkeypatch):
    calls = count_visits(monkeypatch)
    sched = researcher.initial_schedule(sub, cfg, now=clock.t)
    researcher.tick(sub, cfg, sched, now=clock.t)
    clock.advance(minutes=5)
    inst = sub.append("human.instructed", {"text": "x", "kind": "idea"}, subject=h.BENCH, actor={"role": "human"})
    assert researcher.tick(sub, cfg, sched, now=clock.t) == "human"
    assert calls[-1] == ("human", inst)
    assert researcher.tick(sub, cfg, sched, now=clock.t) is None  # answered


def test_gate_blocks_visits_and_logs_once(sub, cfg, clock, monkeypatch):
    calls = count_visits(monkeypatch)
    observe(sub, 90, 10)
    sched = researcher.initial_schedule(sub, cfg, now=clock.t)
    assert researcher.tick(sub, cfg, sched, now=clock.t) is None
    assert researcher.tick(sub, cfg, sched, now=clock.t) is None
    assert calls == [] and len(sched.log) == 1 and "cap" in sched.log[0]
    observe(sub, 10, 10)
    assert researcher.tick(sub, cfg, sched, now=clock.t) == "schedule"


def test_snapshot_refreshes_between_visits(sub, cfg, clock, monkeypatch):
    count_visits(monkeypatch)
    sched = researcher.initial_schedule(sub, cfg, now=clock.t)
    researcher.tick(sub, cfg, sched, now=clock.t)   # visit (mocked: writes no snapshot)
    assert not cfg.snapshot_path.exists()
    clock.advance(minutes=16)
    researcher.tick(sub, cfg, sched, now=clock.t)
    assert json.loads(cfg.snapshot_path.read_text())["generated_at"] == "2026-11-11T12:16:00Z"


# ── real pi against the fake model ─────────────────────────────────────
@pytest.mark.pi
@pytest.mark.skipif(shutil.which("pi") is None, reason="pi CLI not installed")
def test_real_pi_visit_against_fake_model(sub, tmp_path, clock, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    with FakeOpenAI([{"text": json.dumps(decision_json())}]) as fake:
        agent = write_agent_dir(tmp_path / "agent", base_url=fake.base_url, model="fake-model", api_key_env="FAKE_KEY",
                                context_window=200000, max_tokens=4096)
        cfg = researcher.ResearcherConfig(model="fake-model", provider="qwen", agent_dir=agent,
                                          snapshot_path=tmp_path / "snapshot.json")
        res = researcher.run_visit(sub, cfg, trigger="schedule", now=clock.t)
    assert res.error is None, res.error
    assert len(of(sub, "direction")) == 1 and len(of(sub, "lessons.published")) == 1
    sent = json.dumps(fake.requests[0])
    assert '"islands"' in sent.replace("\\", "") and "researcher" in sent
    assert not any(t.get("function", {}).get("name") for t in fake.requests[0].get("tools") or [])
