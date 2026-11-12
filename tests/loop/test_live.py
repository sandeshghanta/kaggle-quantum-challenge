import json
import os
import signal
import threading
from datetime import UTC, datetime, timedelta

import pytest

from loop import config, live
from loop.heartbeat import write_heartbeat
from loop.seed import seed_reference
from substrate.cli import Substrate

PARENT = "sha256:" + "0" * 64
ACTOR = {"role": "test", "harness": "t", "host": "h"}
VER, BASIS = config.verifier_tag(), config.COST_BASIS


@pytest.fixture
def sub(tmp_path):
    s = Substrate(tmp_path / "s")
    seed_reference(s)
    return s


def now():
    return datetime.now(UTC) + timedelta(seconds=5)


def commit(sub, note="tweak", parent=None, island="main"):
    blob = sub.put_blob(note.encode())
    sub.append("program.committed", {"blob": blob, "parent": parent, "benchmark": "invsqrt", "note": note},
               subject="prog:" + blob, actor=ACTOR)
    sub.append("island.joined", {"program": blob, "island": island, "via": "submit"}, subject="prog:" + blob, actor=ACTOR)
    return blob


def measure(sub, prog, cost, passes=True, verifier=VER, basis=BASIS):
    sub.append("candidate.measured", {"program": prog, "circuit": "sha256:" + f"{cost:x}".zfill(64), "cost": cost, "passes": passes,
                                      "benchmark": "invsqrt", "metrics": {"Q": 80, "T": 1000}, "verifier": verifier,
                                      "cost_basis": basis}, subject="prog:" + prog, actor=ACTOR)


def reject(sub, prog, tier="sweep", reason="quick_check_failed", detail="first line\nsecond"):
    sub.append("candidate.rejected", {"program": prog, "circuit": None, "reason": reason, "tier": tier,
                                      "detail": detail}, subject="prog:" + prog, actor=ACTOR)


def test_empty_substrate_has_all_keys(tmp_path):
    out = live.build_live(Substrate(tmp_path / "e"), heartbeats_dir=None, now=now())
    assert set(out) == {"generated_at", "score", "history", "marks", "health", "workers", "activity", "progress",
                        "islands", "claude", "last_visit", "hardware", "alerts", "launcher"}
    assert out["progress"]["verdict"] is None and out["islands"] == [] and out["alerts"] == []
    assert out["hardware"] is None and out["claude"] == {"points": [], "latest": None, "projected_week_pct": None}
    assert out["score"]["best_cost"] is None and out["score"]["reference_cost"] == 2076774
    assert out["score"]["public_best_ratio"] == 15.2 and out["score"]["top_ratio"] == 20.6
    assert out["history"] == [] and out["workers"] == [] and out["activity"] == []
    h = out["health"]
    assert set(h) == {"assistants", "verifier", "spend"}
    assert set(h["assistants"]) == {"running", "attempts_24h", "hit_rate", "sweep_fail_rate"}
    assert set(h["verifier"]) == {"backlog", "oldest_wait_min", "full_checks_24h"}
    assert set(h["spend"]) == {"qwen_tokens_out_24h", "cpu_seconds_24h"}


def test_score_and_history_use_matching_verifier_and_passing_only(sub):
    a, b, c = commit(sub, "a"), commit(sub, "b"), commit(sub, "c")
    measure(sub, a, 200_000)
    measure(sub, b, 150_000)
    measure(sub, c, 100_000, passes=False)             # failing: ignored
    measure(sub, c, 90_000, verifier="other@1")         # wrong verifier: ignored
    measure(sub, a, 170_000)                            # not an improvement
    t = now()
    out = live.build_live(sub, heartbeats_dir=None, now=t)
    assert out["score"]["best_cost"] == 150_000 and out["score"]["Q"] == 80 and out["score"]["T"] == 1000
    assert [p["cost"] for p in out["history"]] == [200_000, 150_000, 150_000]
    assert out["history"][-1]["t"] == t.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out["score"]["ratio_24h_ago"] is None       # nothing measured 24h ago


def test_ratio_24h_ago_uses_best_at_that_time(sub):
    a = commit(sub, "a")
    measure(sub, a, 200_000)
    out = live.build_live(sub, heartbeats_dir=None, now=datetime.now(UTC) + timedelta(hours=25))
    assert out["score"]["ratio_24h_ago"] == pytest.approx(2076774 / 200_000)


def test_marks_label_directions_in_order(sub):
    for i in range(2):
        sub.append("direction", {"brief": f"go {i}", "why": "w", "prediction": {"metric": "island_best_cost", "island": "main", "threshold": 1, "by_attempts": 5}}, subject="bench:invsqrt", actor=ACTOR)
    out = live.build_live(sub, heartbeats_dir=None, now=now())
    assert [m["label"] for m in out["marks"]] == ["D1", "D2"] and out["marks"][0]["t"].endswith("Z")


def test_health_assistants_and_verifier(sub, tmp_path):
    a, b, c = commit(sub, "a", parent=PARENT), commit(sub, "b", parent=PARENT), commit(sub, "c", parent=PARENT)
    for _ in range(4):
        sub.append("attempt.announced", {"island": "main", "plan": "x"}, subject="prog:" + a, actor=ACTOR)
    measure(sub, a, 100_000)
    reject(sub, b, tier="sweep")
    out = live.build_live(sub, heartbeats_dir=None, now=now())
    h = out["health"]
    assert h["assistants"]["attempts_24h"] == 4 and h["assistants"]["hit_rate"] == pytest.approx(0.25)
    assert h["assistants"]["sweep_fail_rate"] == pytest.approx(0.5)      # 1 of 2 judged programs
    assert h["verifier"]["backlog"] == 2                                 # seed + c unjudged
    assert h["verifier"]["oldest_wait_min"] >= 0 and h["verifier"]["full_checks_24h"] == 1


def test_rates_are_none_without_data(sub):
    h = live.build_live(sub, heartbeats_dir=None, now=now())["health"]
    assert h["assistants"]["hit_rate"] is None and h["assistants"]["sweep_fail_rate"] is None
    assert h["verifier"]["oldest_wait_min"] is not None  # the seed is waiting


def test_windows_exclude_old_events(sub):
    a = commit(sub, "a", parent=PARENT)
    sub.append("attempt.announced", {"island": "main", "plan": "x"}, subject="prog:" + a, actor=ACTOR)
    measure(sub, a, 100_000)
    sub.append("budget.spent", {"pool": "qwen", "tokens_in": 1, "tokens_out": 50}, subject="pool:qwen", actor=ACTOR)
    h = live.build_live(sub, heartbeats_dir=None, now=datetime.now(UTC) + timedelta(hours=25))["health"]
    assert h["assistants"]["attempts_24h"] == 0 and h["verifier"]["full_checks_24h"] == 0
    assert h["spend"]["qwen_tokens_out_24h"] == 0


def test_spend(sub):
    sub.append("budget.spent", {"pool": "qwen", "tokens_in": 1, "tokens_out": 50}, subject="pool:qwen", actor=ACTOR)
    sub.append("budget.spent", {"pool": "qwen", "tokens_in": 1, "tokens_out": 25}, subject="pool:qwen", actor=ACTOR)
    sub.append("budget.spent", {"pool": "cpu", "tokens_in": 0, "tokens_out": 0, "cpu_seconds": 12.5},
               subject="pool:cpu", actor=ACTOR)
    s = live.build_live(sub, heartbeats_dir=None, now=now())["health"]["spend"]
    assert s == {"qwen_tokens_out_24h": 75, "cpu_seconds_24h": 12.5}


def test_workers_sorted_stale_and_running(tmp_path, sub):
    hb = tmp_path / "hb"
    write_heartbeat(hb, 10, state="thinking")
    write_heartbeat(hb, 2, state="idle")
    old = json.loads((hb / "worker-2.json").read_text())
    old["updated_at"] = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (hb / "worker-2.json").write_text(json.dumps(old))
    (hb / "worker-3.json").write_text("{not json")
    (hb / "notes.txt").write_text("x")
    out = live.build_live(sub, heartbeats_dir=hb, now=now())
    assert [w["worker"] for w in out["workers"]] == [2, 10]
    assert out["workers"][0]["stale"] is True and out["workers"][1]["stale"] is False
    assert out["health"]["assistants"]["running"] == 1


def test_missing_heartbeat_dir_is_fine(sub, tmp_path):
    assert live.build_live(sub, heartbeats_dir=tmp_path / "nope", now=now())["workers"] == []


def test_activity_kinds_newest_first_and_capped(sub):
    seed_prog = sub.history("bench:invsqrt", types=["island.created"])  # noqa: F841
    a = commit(sub, "tightened step", parent=PARENT)
    reject(sub, a, tier="sweep", reason="quick_check_failed", detail="bad x\nmore")
    measure(sub, a, 150_000)
    measure(sub, a, 160_000)
    sub.append("direction", {"brief": "go left", "why": "w", "prediction": {"metric": "island_best_cost", "island": "main", "threshold": 1, "by_attempts": 5}}, subject="bench:invsqrt", actor=ACTOR)
    sub.append("lessons.published", {"working": ["x"], "not_working": ["y", "z"]}, subject="bench:invsqrt", actor=ACTOR)
    act = live.build_live(sub, heartbeats_dir=None, now=now())["activity"]
    assert [x["kind"] for x in act][:6] == ["lessons", "direction", "measured", "new best", "rejected", "committed"]
    by = {x["kind"]: x["text"] for x in reversed(act)}  # newest of each kind
    assert by["committed"] == "main: tightened step"
    assert by["rejected"] == "quick_check_failed (sweep): bad x"
    assert "160,000" in by["measured"] and "passes" in by["measured"]
    assert "150,000" in by["new best"] and by["direction"] == "go left"
    assert all(x["t"].endswith("Z") for x in act)
    for i in range(40):
        reject(sub, a, tier="gate", reason="not_better")
    assert len(live.build_live(sub, heartbeats_dir=None, now=now())["activity"]) == 25


def test_json_serialisable(sub):
    json.dumps(live.build_live(sub, heartbeats_dir=None, now=now()), allow_nan=False)


def test_write_atomic_readable_by_group(tmp_path):
    out = tmp_path / "d" / "live.json"
    live.write_atomic(out, {"a": 1})
    assert json.loads(out.read_text()) == {"a": 1}
    assert (os.stat(out).st_mode & 0o777) == 0o644 and [p.name for p in out.parent.iterdir()] == ["live.json"]


def test_main_once_writes_file(sub, tmp_path):
    out = tmp_path / "live.json"
    assert live.main(["--substrate", str(sub.root), "--out", str(out), "--once"]) == 0
    assert "score" in json.loads(out.read_text())


def test_main_loops_and_drains_on_sigterm(sub, tmp_path):
    out = tmp_path / "live.json"
    t = threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGTERM))
    t.start()
    old = signal.getsignal(signal.SIGTERM)
    try:
        assert live.main(["--substrate", str(sub.root), "--out", str(out), "--interval-s", "0.2"]) == 0
    finally:
        t.cancel()
        signal.signal(signal.SIGTERM, old)
    assert out.exists()


def test_main_survives_a_failing_build(sub, tmp_path, monkeypatch):
    out = tmp_path / "live.json"
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        raise RuntimeError("boom")
    monkeypatch.setattr(live, "build_live", flaky)
    assert live.main(["--substrate", str(sub.root), "--out", str(out), "--once"]) == 0 and calls and not out.exists()



# ── progress verdict ───────────────────────────────────────────────────
def hist(*hours_ago, now_=None):
    n = now_ or datetime(2026, 11, 11, 12, tzinfo=UTC)
    return [{"t": live._iso(n - timedelta(hours=h)), "cost": 100 - i, "new": True} for i, h in enumerate(hours_ago)]


@pytest.mark.parametrize("last_h,verdict", [(1, "improving"), (6, "improving"), (9, "slowing"), (24, "plateau")])
def test_progress_verdict(last_h, verdict):
    n = datetime(2026, 11, 11, 12, tzinfo=UTC)
    p = live.progress(hist(48, last_h, now_=n), n)
    assert p["verdict"] == verdict and len(p["per_day"]) == 7


def test_progress_counts_improvements_per_day_excluding_the_seed():
    n = datetime(2026, 11, 11, 12, tzinfo=UTC)
    p = live.progress(hist(30, 2, 1, now_=n), n)  # seed yesterday, two gains today
    assert [d["count"] for d in p["per_day"]][-2:] == [0, 2]


def test_progress_from_build_live(sub):
    a = commit(sub)
    measure(sub, a, 1000)
    out = live.build_live(sub, heartbeats_dir=None, now=now())
    assert out["progress"]["verdict"] == "improving" and out["progress"]["last_gain"] is None  # only the first best


# ── islands ────────────────────────────────────────────────────────────
def test_island_effort_last_hour(sub):
    for _ in range(3):
        sub.append("attempt.announced", {"island": "main", "plan": "main: improving parent ab"},
                   subject="prog:" + PARENT, actor=ACTOR)
    a, b = commit(sub, "a"), commit(sub, "b")
    measure(sub, a, 900)
    reject(sub, b)
    [row] = live.build_live(sub, heartbeats_dir=None, now=now())["islands"]
    assert row == {"island": "main", "attempts_1h": 3, "judged_1h": 2, "pass_rate_1h": 0.5, "best_cost": 900}


# ── Claude usage ───────────────────────────────────────────────────────
def quota_at(t, five, seven, seven_reset):
    return (t, {"five_hour_pct": five, "seven_day_pct": seven, "seven_day_resets_at": live._iso(seven_reset)})


def test_claude_projection_needs_a_day_of_the_week():
    n = datetime(2026, 11, 11, 12, tzinfo=UTC)
    reset = n + timedelta(days=6, hours=12)  # 12 h into the week
    assert live.claude_usage([quota_at(n, 10, 5, reset)], n)["projected_week_pct"] is None
    reset = n + timedelta(days=3, hours=12)  # half way: 30% now -> 60% at reset
    c = live.claude_usage([quota_at(n, 10, 30, reset)], n)
    assert c["projected_week_pct"] == 60.0 and c["latest"]["seven_day_pct"] == 30 and len(c["points"]) == 1


# ── alerts ─────────────────────────────────────────────────────────────
class FakeMonitor(live.Monitor):
    def __init__(self, *, down_since=None, disk=None, tps=None, cpu=None):
        super().__init__()
        self.qwen_down_since, self._disk, self._avg = down_since, disk, {"qwen_tps": tps, "cpu_pct": cpu}

    def latest(self):
        return {"disk_pct": self._disk}

    def average(self, key, window, now):
        return self._avg.get(key)


OK_VERIFIER = {"backlog": 0, "oldest_wait_min": 0, "full_checks_24h": 0}
N = datetime(2026, 11, 11, 12, tzinfo=UTC)


def rules(**kw):
    args = {"workers": [], "verifier": OK_VERIFIER, "last_visit": N, "claude": {}, "monitor": FakeMonitor(),
            "now": N} | kw
    return [a["rule"] for a in live.alerts(**args)]


def test_no_alerts_when_all_is_well():
    assert rules(monitor=FakeMonitor(disk=30, tps=140, cpu=85), claude={"projected_week_pct": 75}) == []


def test_each_alert_rule_fires():
    old = live._iso(N - timedelta(minutes=6))
    assert rules(workers=[{"worker": 1, "updated_at": old}]) == ["worker_down"]
    assert rules(workers=[{"worker": 1, "updated_at": live._iso(N - timedelta(minutes=3))}]) == []
    assert rules(monitor=FakeMonitor(down_since=N - timedelta(minutes=3), tps=10)) == ["qwen_unreachable"]
    assert rules(verifier={"backlog": 21, "oldest_wait_min": 5, "full_checks_24h": 0}) == ["verifier_stuck"]
    assert rules(verifier={"backlog": 2, "oldest_wait_min": 31, "full_checks_24h": 0}) == ["verifier_stuck"]
    assert rules(last_visit=N - timedelta(hours=3)) == ["researcher_stuck"]
    assert rules(last_visit=N - timedelta(hours=3), claude={"latest": {"five_hour_pct": 85}}) == []  # capped: expected
    assert rules(monitor=FakeMonitor(disk=90)) == ["disk_full"]
    assert rules(monitor=FakeMonitor(tps=40)) == ["spark_underused"]
    assert rules(monitor=FakeMonitor(cpu=30)) == ["devbox_underused"]
    assert rules(claude={"projected_week_pct": 40}) == ["claude_underused"]


def test_build_live_reports_hardware_and_alerts(sub, tmp_path):
    m = FakeMonitor(cpu=20)
    m.mem_total_gib = 25.0
    out = live.build_live(sub, heartbeats_dir=None, now=now(), monitor=m)
    assert out["hardware"]["mem_total_gib"] == 25.0 and "devbox_underused" in [a["rule"] for a in out["alerts"]]


# ── launcher (Compose deployments) ─────────────────────────────────────
def test_launcher_alerts(tmp_path):
    fresh = live._iso(N - timedelta(seconds=10))
    ok = {"updated_at": fresh, "slots": [{"slot": 1, "state": "running"}]}
    assert rules(launcher=ok, launcher_expected=True) == []
    backoff = {"updated_at": fresh, "slots": [{"slot": 2, "state": "backoff", "last_exit": 3}]}
    assert rules(launcher=backoff, launcher_expected=True) == ["worker_down"]
    stale = {"updated_at": live._iso(N - timedelta(minutes=5)), "slots": []}
    assert rules(launcher=stale, launcher_expected=True) == ["worker_down"]
    assert rules(launcher=None, launcher_expected=True) == ["worker_down"]
    assert rules(launcher=None) == []  # systemd deployments: no launcher expected


def test_build_live_reads_the_launcher_file(sub, tmp_path):
    import json as _json
    f = tmp_path / "launcher.json"
    f.write_text(_json.dumps({"updated_at": live._iso(datetime.now(UTC)), "slots": [{"slot": 1, "state": "running"}]}))
    out = live.build_live(sub, heartbeats_dir=None, now=now(), launcher_status=f)
    assert out["launcher"]["slots"][0]["state"] == "running" and out["alerts"] == []
