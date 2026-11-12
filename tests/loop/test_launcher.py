import json
from datetime import UTC, datetime, timedelta

from loop import launcher

SPEC = launcher.JobSpec(image="sha256:img", networks=["rl-substrate", "rl-qwen"], token_file="/srv/secrets/worker",
                        substrate="http://substrate:7070", base_url="http://qwen-proxy:8000/v1", model="qwen")


def test_job_command_is_fixed_and_locked_down():
    cmd = launcher.job_command(SPEC, 3)
    assert cmd[:2] == ["docker", "create"]
    for flag in ("--read-only", "--cap-drop", "--pids-limit", "--memory", "--cpus"):
        assert flag in cmd
    assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert "/work:rw,size=1g" in cmd and "/srv/secrets/worker:/run/secrets/worker:ro" in cmd
    assert cmd[cmd.index("--network") + 1] == "rl-substrate" and cmd.count("--network") == 1
    assert "LOOP_SANDBOX=container" in cmd and f"{launcher.LABEL}.slot=3" in cmd
    tail = cmd[cmd.index("sha256:img"):]
    assert tail[:4] == ["sha256:img", "python", "-m", "loop.worker"] and "--once" in tail
    assert tail[tail.index("--id") + 1] == "3" and tail[tail.index("--workroot") + 1] == "/work"


class FakeRuntime:
    binary = "docker"

    def __init__(self):
        self.containers: dict[str, dict] = {}
        self.started, self.stopped, self.removed = [], [], []
        self.n = 0

    def jobs(self, project):
        return list(self.containers.values())

    def start(self, cmd, extra):
        self.n += 1
        cid = f"c{self.n}"
        slot = int(cmd[cmd.index("--id") + 1])
        self.containers[cid] = {"id": cid, "slot": slot, "running": True, "exit_code": None,
                                "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")}
        self.started.append((slot, extra))
        return cid

    def finish(self, cid, code):
        self.containers[cid].update(running=False, exit_code=code)

    def remove(self, cid):
        self.removed.append(cid)
        self.containers.pop(cid, None)

    def stop(self, cid, grace_s=30):
        self.stopped.append(cid)

    def logs(self, cid, tail=5):
        return [f"worker: done in {cid}"]


def test_fills_every_slot_and_connects_the_extra_networks():
    rt, slots = FakeRuntime(), [launcher.Slot(i) for i in (1, 2)]
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=0)
    assert [s for s, _ in rt.started] == [1, 2] and rt.started[0][1] == ["rl-qwen"]
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=1)
    assert len(rt.started) == 2  # still running: nothing new


def test_finished_job_is_removed_and_replaced_with_a_fresh_container():
    rt, slots = FakeRuntime(), [launcher.Slot(1)]
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=0)
    rt.finish("c1", 0)
    log = launcher.tick(rt, SPEC, slots, max_job_s=1200, now=1)
    assert rt.removed == ["c1"] and list(rt.containers) == ["c2"] and slots[0].jobs == 1
    assert "slot 1: worker: done in c1" in log


def test_fast_failures_back_off_and_a_success_resets():
    rt, slots = FakeRuntime(), [launcher.Slot(1)]
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=0)
    rt.finish("c1", launcher.EXIT_FAILED)
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=1)
    assert slots[0].state == "backoff" and "c2" not in rt.containers
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=1 + 5.1)
    assert "c2" in rt.containers
    rt.finish("c2", 0)
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=7)
    assert slots[0].fast_failures == 0 and "c3" in rt.containers


def test_waiting_exit_pauses_the_slot():
    rt, slots = FakeRuntime(), [launcher.Slot(1)]
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=0)
    rt.finish("c1", launcher.EXIT_WAITING)
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=1)
    assert slots[0].state == "waiting" and len(rt.containers) == 0
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=1 + launcher.WAITING_S)
    assert len(rt.containers) == 1


def test_overdue_job_is_stopped():
    rt, slots = FakeRuntime(), [launcher.Slot(1)]
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=0)
    old = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000000001Z")
    rt.containers["c1"]["started_at"] = old
    launcher.tick(rt, SPEC, slots, max_job_s=1200, now=1)
    assert rt.stopped == ["c1"]


def test_jobs_of_retired_slots_are_removed():
    rt = FakeRuntime()
    launcher.tick(rt, SPEC, [launcher.Slot(1), launcher.Slot(2)], max_job_s=1200, now=0)
    launcher.tick(rt, SPEC, [launcher.Slot(1)], max_job_s=1200, now=1)
    assert rt.stopped == ["c2"] and "c2" in rt.removed


def test_status_file(tmp_path):
    slots = [launcher.Slot(1, state="backoff", last_exit=3, fast_failures=2)]
    launcher.write_status(tmp_path / "launcher.json", slots)
    d = json.loads((tmp_path / "launcher.json").read_text())
    assert d["slots"][0]["state"] == "backoff" and "resume_at" not in d["slots"][0] and d["updated_at"]
