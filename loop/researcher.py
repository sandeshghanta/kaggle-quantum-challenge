"""The researcher service: wakes on a schedule (or on a human instruction), runs one visit if the budget allows.

No model of its own: this is plain Python. A visit builds the digest from the substrate, shows it to Claude
Opus through pi (single shot, no tools), and records the answer as events through loop.decide. The model never
writes files. After every visit researcher.json is rewritten for the status page.

The pi run is NOT wrapped in the bubblewrap sandbox: it has no tools (nothing it says can touch the machine) and
the pi-claude-bridge extension needs the owner's Claude login in HOME.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loop import config, decide, digest, researcher_view
from loop.pi_runner import PiRun, run_pi, write_agent_dir
from substrate import connect
from substrate.cli import Substrate

DEFAULT_ROLE_DIR = config.REPO / "roles" / "researcher" / "v1"
SUBJECT = "bench:" + config.BENCHMARK
POLL_S = 30
FAILURE_COOLDOWN = timedelta(minutes=5)
BLIND_INTERVAL = timedelta(hours=5)  # one plan window: the most often a visit may run without a usage reading


@dataclass
class ResearcherConfig:
    model: str
    provider: str
    agent_dir: Path
    role_dir: Path = DEFAULT_ROLE_DIR
    status_out: Path | None = None
    interval_min: float = 45
    cap_pct: float = 80
    status_interval_min: float = 15


@dataclass(frozen=True)
class VisitResult:
    visit_id: str
    event_ids: list[str]
    error: str | None
    run: PiRun | None
    quota_event: str | None = None


# ── budget gate ────────────────────────────────────────────────────────
def gate(sub: Substrate, *, now: datetime, cap_pct: float = 80) -> tuple[bool, str]:
    """May a visit run? Both plan windows must be below `cap_pct` in the latest quota.observed.

    A window whose reset time has passed counts as fresh. Readings come from visits, so when the last visit
    produced none (or there has never been one) visits are "blind" and limited to one per BLIND_INTERVAL.
    """
    q = digest.latest_quota(sub)
    last = digest.last_visit(sub)
    if last is not None and (q is None or digest.parse_ts(q["t"]) < digest.parse_ts(last.ts)):
        if now - digest.parse_ts(last.ts) < BLIND_INTERVAL:
            return False, "no usage reading since the last visit; one visit per 5 h until there is one"
        return True, "no usage reading; blind visit"
    if q is None:
        return True, "no visit yet"
    for name, pct, resets in (("five_hour", q["five_hour_pct"], q.get("five_hour_resets_at")),
                              ("seven_day", q["seven_day_pct"], q.get("seven_day_resets_at"))):
        if resets and digest.parse_ts(resets) <= now:
            continue
        if pct >= cap_pct:
            return False, f"{name} usage {pct:g}% is at or above the {cap_pct:g}% cap"
    return True, "below cap"


# ── quota extraction (best effort) ─────────────────────────────────────
def _pct(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(min(100.0, max(0.0, x * 100 if x <= 1 else x)), 2)  # headers give a fraction; 1.0 is read as 100%


def _reset(v) -> str | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return digest.iso(datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC))
    if isinstance(v, str):
        try:
            return digest.iso(datetime.fromtimestamp(float(v), UTC))
        except ValueError:
            try:
                return digest.iso(digest.parse_ts(v))
            except ValueError:
                return None
    return None


class QuotaScan:
    """Looks through pi's JSON events for Claude plan usage.

    UNCONFIRMED against the real pi-claude-bridge: it recognises (a) keys ending in `unified-5h-utilization`,
    `unified-7d-utilization` and the matching `-reset` keys (Anthropic rate-limit response headers, any case, `_` or
    `-`), and (b) a `rate_limit` / `rate_limit_info` object with a `rateLimitType` of five_hour / seven_day, a
    `utilization` and a `resetsAt` (Agent SDK style). If it finds both windows it yields a quota.observed payload.
    """

    def __init__(self):
        self.found: dict[str, object] = {}

    def feed(self, ev) -> None:
        if isinstance(ev, dict):
            for key in ("rate_limit", "rate_limit_info", "rateLimitInfo", "rateLimit"):
                if isinstance(ev.get(key), dict):
                    self._sdk(ev[key])
            for k, v in ev.items():
                nk = str(k).lower().replace("_", "-")
                for window, tag in (("five_hour", "5h"), ("seven_day", "7d")):
                    if nk.endswith(f"unified-{tag}-utilization") and _pct(v) is not None:
                        self.found[f"{window}_pct"] = _pct(v)
                    elif nk.endswith(f"unified-{tag}-reset") and _reset(v):
                        self.found[f"{window}_resets_at"] = _reset(v)
                self.feed(v)
        elif isinstance(ev, list):
            for x in ev:
                self.feed(x)

    def _sdk(self, o: dict) -> None:
        kind = str(o.get("rateLimitType") or o.get("rate_limit_type") or "").lower().replace("-", "_")
        window = {"five_hour": "five_hour", "seven_day": "seven_day"}.get(kind)
        if window and _pct(o.get("utilization")) is not None:
            self.found[f"{window}_pct"] = _pct(o["utilization"])
            if _reset(o.get("resetsAt") or o.get("resets_at")):
                self.found[f"{window}_resets_at"] = _reset(o.get("resetsAt") or o.get("resets_at"))

    def result(self) -> dict | None:
        if "five_hour_pct" not in self.found or "seven_day_pct" not in self.found:
            return None
        return {k: v for k, v in self.found.items()}


def write_status(sub: Substrate, cfg: ResearcherConfig, *, now: datetime, next_visit: datetime | None) -> None:
    """Write researcher.json (this service's only status output; live data is another service's live.json)."""
    researcher_view.write_view(cfg.status_out, researcher_view.build_researcher_view(
        sub, now=now, cap_pct=cfg.cap_pct, next_visit=next_visit))


# ── one visit ──────────────────────────────────────────────────────────
def run_visit(sub: Substrate, cfg: ResearcherConfig, *, trigger: str, cause: str | None = None,
              now: datetime) -> VisitResult:
    """One researcher visit. The caller has already checked the budget gate."""
    role = json.loads((cfg.role_dir / "role.json").read_text())
    actor = config.actor("researcher", role["version"], cfg.model)

    data = json.dumps(digest.build_digest(sub, now=now), sort_keys=True).encode()
    visit = sub.append("researcher.visited", {"trigger": trigger, "digest": sub.put_blob(data), "model": cfg.model},
                       subject=SUBJECT, actor=actor, cause=cause)
    prompt = (cfg.role_dir / "task.md").read_text().replace("{digest}", data.decode())

    scan = QuotaScan()
    with tempfile.TemporaryDirectory(prefix="researcher-") as work:
        run = run_pi(Path(work), cfg.agent_dir, prompt=prompt, system_append=cfg.role_dir / "system.md", tools=[],
                     model=cfg.model, timeout_s=role["timeout_s"], provider=cfg.provider, sandboxed=False,
                     on_event=scan.feed)
    sub.append("budget.spent", {"pool": "claude", "tokens_in": run.tokens_in, "tokens_out": run.tokens_out,
                                "tokens_cached": run.tokens_cached, "wall_seconds": round(run.seconds, 2)},
               subject="pool:claude", actor=actor, cause=visit)

    ids: list[str] = []
    if run.ok:
        ids, error = decide.parse_and_apply(sub, run.final_text, visit_id=visit, actor=actor)
    else:
        error = f"pi failed: {run.error}"

    quota_event = None
    if (quota := scan.result()) is not None:
        quota_event = sub.append("quota.observed", quota, subject="pool:claude", actor=actor, cause=visit)

    if cfg.status_out is not None:
        write_status(sub, cfg, now=now, next_visit=now + timedelta(minutes=cfg.interval_min))
    return VisitResult(visit, ids, error, run, quota_event)


# ── service loop ───────────────────────────────────────────────────────
@dataclass
class Schedule:
    next_due: datetime
    next_snapshot: datetime
    cooldown_until: datetime | None = None
    last_blocked: str | None = None
    log: list[str] = field(default_factory=list)


def initial_schedule(sub: Substrate, cfg: ResearcherConfig, *, now: datetime) -> Schedule:
    last = digest.last_visit(sub)
    due = digest.parse_ts(last.ts) + timedelta(minutes=cfg.interval_min) if last else now
    return Schedule(next_due=due, next_snapshot=now)


def tick(sub: Substrate, cfg: ResearcherConfig, sched: Schedule, *, now: datetime) -> str | None:
    """One pass of the service: maybe run a visit, maybe refresh the snapshot. Returns 'schedule'/'human'/None."""
    ran = None
    waiting = digest.unanswered_instructions(sub)
    cooling = sched.cooldown_until is not None and now < sched.cooldown_until
    if (waiting or now >= sched.next_due) and not cooling:
        allowed, reason = gate(sub, now=now, cap_pct=cfg.cap_pct)
        if allowed:
            trigger = "human" if waiting else "schedule"
            sched.last_blocked = None
            sched.next_due = now + timedelta(minutes=cfg.interval_min)
            result = run_visit(sub, cfg, trigger=trigger, cause=waiting[-1].id if waiting else None, now=now)
            sched.log.append(f"visit {result.visit_id} ({trigger}): "
                             + (f"error: {result.error}" if result.error else f"{len(result.event_ids)} events"))
            ran = trigger
            sched.next_snapshot = now + timedelta(minutes=cfg.status_interval_min)
        elif reason != sched.last_blocked:
            sched.last_blocked = reason
            sched.log.append(f"visit skipped: {reason}")
    if (cfg.status_out is not None and cfg.status_interval_min > 0 and now >= sched.next_snapshot
            and ran is None):
        write_status(sub, cfg, now=now, next_visit=sched.next_due)
        sched.next_snapshot = now + timedelta(minutes=cfg.status_interval_min)
    return ran


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--substrate", "--root", dest="substrate", required=True,
                   help="data directory (in-process) or unix:/run/substrate/api.sock")
    p.add_argument("--interval-min", type=float, default=45)
    p.add_argument("--cap-pct", type=float, default=80)
    p.add_argument("--provider", default="claude-bridge")
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--agent-dir", required=True,
                   help="persistent pi agent dir (production: the pi-claude-bridge extension is installed here)")
    p.add_argument("--status-out", help="path of researcher.json, rewritten after each visit")
    p.add_argument("--status-interval-min", type=float, default=15,
                   help="also refresh researcher.json this often between visits (0 = only after visits)")
    p.add_argument("--role-dir", default=str(DEFAULT_ROLE_DIR))
    p.add_argument("--base-url", help="tests: OpenAI-compatible endpoint; writes models.json into --agent-dir")
    p.add_argument("--api-key-env", help="tests: env var holding the endpoint's API key (with --base-url)")
    args = p.parse_args(argv)

    if args.base_url:
        if not args.api_key_env:
            p.error("--base-url needs --api-key-env")
        write_agent_dir(Path(args.agent_dir), base_url=args.base_url, model=args.model, api_key_env=args.api_key_env,
                        context_window=200000, max_tokens=16000, provider=args.provider)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        if args.provider == "claude-bridge" and os.environ.get(var):
            p.error(f"{var} is set: the researcher must use the Claude Pro login, not an API key")

    stopping = False

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True
        print("researcher: stop requested; finishing the current visit", flush=True)

    signal.signal(signal.SIGTERM, on_term)
    cfg = ResearcherConfig(model=args.model, provider=args.provider, agent_dir=Path(args.agent_dir),
                           role_dir=Path(args.role_dir), status_out=Path(args.status_out) if args.status_out else None,
                           interval_min=args.interval_min, cap_pct=args.cap_pct,
                           status_interval_min=args.status_interval_min)
    sub = connect(args.substrate)
    if isinstance(sub, Substrate):
        sub.init()
    sched = initial_schedule(sub, cfg, now=datetime.now(UTC))
    while not stopping:
        now = datetime.now(UTC)
        try:
            tick(sub, cfg, sched, now=now)
        except Exception as e:  # keep serving; a failed visit must not turn into a hot loop
            sched.cooldown_until = now + FAILURE_COOLDOWN
            sched.log.append(f"error: {type(e).__name__}: {e}")
        while sched.log:
            print(f"researcher: {sched.log.pop(0)}", flush=True)
        end = time.monotonic() + POLL_S
        while not stopping and time.monotonic() < end:
            time.sleep(0.5)
    print("researcher: exiting", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
