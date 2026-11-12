"""Launcher: keeps N disposable lab-assistant job containers running.

Each job runs in a fresh container from the loop image (read-only root, tmpfs work dir, no capabilities,
CPU/memory/process limits, the worker token) and does exactly one job (`loop.worker --once`). When it exits
the launcher reads its exit code, removes the container (and with it everything the job wrote) and starts a
fresh one. Slots whose jobs fail fast back off; jobs that outlive their budget are killed.

This is the only component with access to the container runtime. It starts one fixed image with fixed
arguments and takes no input from jobs. It never touches the substrate.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

LABEL = "research-loop.job"
EXIT_FAILED, EXIT_WAITING = 3, 4  # loop.worker --once
BACKOFF_MAX_S = 300.0
WAITING_S = 30.0


@dataclass
class JobSpec:
    image: str
    networks: list[str]
    token_file: str              # host path of the worker token, bind-mounted read-only
    substrate: str               # e.g. http://substrate:7070
    base_url: str                # e.g. http://qwen-proxy:8000/v1
    model: str
    context_window: int = 32768
    memory: str = "3g"
    cpus: str = "2"
    pids: int = 512
    tmpfs_size: str = "1g"
    project: str = "research-loop"


def job_command(spec: JobSpec, slot: int, *, runtime: str = "docker") -> list[str]:
    """The fixed `docker create` for one job in `slot` (started after its extra networks are connected)."""
    cmd = [runtime, "create",
           "--name", f"{spec.project}-job-{slot}-{int(time.time())}",
           "--label", f"{LABEL}={spec.project}", "--label", f"{LABEL}.slot={slot}",
           "--read-only", "--tmpfs", f"/work:rw,size={spec.tmpfs_size}", "--tmpfs", "/tmp:rw,size=256m",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--memory", spec.memory, "--cpus", spec.cpus, "--pids-limit", str(spec.pids),
           "-v", f"{spec.token_file}:/run/secrets/worker:ro",
           "-e", "LOOP_SANDBOX=container", "-e", "SUBSTRATE_TOKEN_FILE=/run/secrets/worker",
           "-e", "RESEARCH_LOOP_API_KEY=none", "-e", "HOME=/tmp"]
    for net in spec.networks[:1]:  # create takes one network; the rest are connected before start
        cmd += ["--network", net]
    return cmd + [spec.image, "python", "-m", "loop.worker", "--once", "--substrate", spec.substrate,
                  "--id", str(slot), "--base-url", spec.base_url, "--model", spec.model,
                  "--api-key-env", "RESEARCH_LOOP_API_KEY", "--context-window", str(spec.context_window),
                  "--workroot", "/work"]


@dataclass
class Slot:
    slot: int
    state: str = "idle"          # running | backoff | waiting | idle
    container: str | None = None
    started_at: str | None = None
    last_exit: int | None = None
    fast_failures: int = 0
    jobs: int = 0
    resume_at: float = field(default=0.0, repr=False)


def backoff_s(fast_failures: int) -> float:
    return 0.0 if fast_failures <= 0 else min(BACKOFF_MAX_S, 5.0 * 2 ** (fast_failures - 1))


class Runtime:
    """The few runtime calls the launcher needs, via the docker CLI (works with Podman's compatible socket)."""

    def __init__(self, binary: str = "docker"):
        self.binary = binary

    def _run(self, *args: str, check: bool = True) -> str:
        return subprocess.run([self.binary, *args], capture_output=True, text=True, check=check,
                              timeout=120).stdout.strip()

    def jobs(self, project: str) -> list[dict]:
        """[{id, slot, running, exit_code, started_at}] for this project's job containers."""
        ids = self._run("ps", "-aq", "--filter", f"label={LABEL}={project}").split()
        if not ids:
            return []
        out = []
        for c in json.loads(self._run("inspect", *ids)):
            st = c["State"]
            out.append({"id": c["Id"][:12], "slot": int(c["Config"]["Labels"].get(f"{LABEL}.slot", -1)),
                        "running": st["Running"], "exit_code": st.get("ExitCode"),
                        "started_at": st.get("StartedAt")})
        return out

    def start(self, create_cmd: list[str], extra_networks: list[str]) -> str:
        cid = subprocess.run(create_cmd, capture_output=True, text=True, check=True, timeout=120).stdout.strip()
        for net in extra_networks:
            self._run("network", "connect", net, cid)
        self._run("start", cid)
        return cid[:12]

    def remove(self, cid: str) -> None:
        self._run("rm", "-f", cid, check=False)

    def logs(self, cid: str, tail: int = 5) -> list[str]:
        r = subprocess.run([self.binary, "logs", "--tail", str(tail), cid], capture_output=True, text=True,
                           timeout=60)
        return (r.stdout + r.stderr).strip().splitlines()

    def stop(self, cid: str, grace_s: int = 30) -> None:
        self._run("stop", "-t", str(grace_s), cid, check=False)


def _age_s(started_at: str | None) -> float:
    """Seconds since a runtime timestamp like 2026-11-12T18:30:00.123456789Z (always UTC)."""
    if not started_at or started_at.startswith("0001"):
        return 0.0
    ts = datetime.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


def tick(rt: Runtime, spec: JobSpec, slots: list[Slot], *, max_job_s: float, now: float) -> list[str]:
    """One pass: reap finished jobs, kill overdue ones, fill empty slots. Returns log lines."""
    log = []
    by_slot = {s.slot: s for s in slots}
    seen = set()
    for job in rt.jobs(spec.project):
        s = by_slot.get(job["slot"])
        if s is None:  # a slot we no longer have (worker count lowered): retire it
            rt.stop(job["id"])
            rt.remove(job["id"])
            continue
        if job["running"]:
            seen.add(s.slot)
            s.state, s.container = "running", job["id"]
            if _age_s(job["started_at"]) > max_job_s:
                log.append(f"slot {s.slot}: job {job['id']} over {max_job_s:.0f}s, killing")
                rt.stop(job["id"], grace_s=10)
            continue
        code = job["exit_code"]
        for line in rt.logs(job["id"]):  # the container is about to go: keep its last words in our log
            log.append(f"slot {s.slot}: {line}")
        rt.remove(job["id"])
        s.jobs += 1
        s.last_exit, s.container = code, None
        if code == EXIT_WAITING:
            s.state, s.resume_at = "waiting", now + WAITING_S
        elif code == 0:
            s.fast_failures, s.state = 0, "idle"
        else:  # EXIT_FAILED, a crash or a kill: back off progressively
            s.fast_failures += 1
            s.state, s.resume_at = "backoff", now + backoff_s(s.fast_failures)
            log.append(f"slot {s.slot}: job exited {code}; backing off {backoff_s(s.fast_failures):.0f}s")
    for s in slots:
        if s.slot in seen or now < s.resume_at:
            continue
        try:
            s.container = rt.start(job_command(spec, s.slot, runtime=rt.binary), spec.networks[1:])
            s.state, s.started_at = "running", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except subprocess.SubprocessError as e:
            s.fast_failures += 1
            s.state, s.resume_at = "backoff", now + backoff_s(s.fast_failures)
            log.append(f"slot {s.slot}: could not start a job: {e}")
    return log


def write_status(path: Path | None, slots: list[Slot]) -> None:
    if path is None:
        return
    data = {"updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "slots": [{k: v for k, v in asdict(s).items() if k != "resume_at"} for s in slots]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "8")))
    p.add_argument("--image", required=True)
    p.add_argument("--networks", required=True, help="comma-separated networks for job containers")
    p.add_argument("--token-file", required=True, help="host path of the worker token file")
    p.add_argument("--substrate", default="http://substrate:7070")
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--context-window", type=int, default=32768)
    p.add_argument("--memory", default="3g")
    p.add_argument("--cpus", default="2")
    p.add_argument("--project", default="research-loop")
    p.add_argument("--max-job-s", type=float, default=1200, help="kill a job running longer than this")
    p.add_argument("--status", help="where to write launcher.json for the status page")
    p.add_argument("--runtime", default="docker")
    p.add_argument("--poll-s", type=float, default=5)
    args = p.parse_args(argv)

    rt = Runtime(args.runtime)
    image = rt._run("image", "inspect", "--format", "{{.Id}}", args.image)  # pin: a rebuild never swaps running jobs
    spec = JobSpec(image=image, networks=args.networks.split(","), token_file=args.token_file,
                   substrate=args.substrate, base_url=args.base_url, model=args.model,
                   context_window=args.context_window, memory=args.memory, cpus=args.cpus, project=args.project)
    slots = [Slot(i) for i in range(1, args.workers + 1)]
    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_term)
    print(f"launcher: {args.workers} job slots, image {image[:19]}", flush=True)
    while not stopping:
        try:
            for line in tick(rt, spec, slots, max_job_s=args.max_job_s, now=time.monotonic()):
                print(f"launcher: {line}", flush=True)
            write_status(Path(args.status) if args.status else None, slots)
        except Exception as e:  # keep the slots alive through a transient runtime error
            print(f"launcher: tick failed: {type(e).__name__}: {e}", flush=True)
        end = time.monotonic() + args.poll_s
        while not stopping and time.monotonic() < end:
            time.sleep(0.2)
    print("launcher: stopping jobs (their work is abandoned; announcements stay as orphans)", flush=True)
    for job in rt.jobs(spec.project):
        rt.stop(job["id"])
        rt.remove(job["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
