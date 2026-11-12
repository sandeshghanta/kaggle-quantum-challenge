"""Machine metrics for the status page: devbox CPU / memory / disk and Qwen's serving metrics.

Owned by the live updater. Nothing here touches the substrate: samples live in memory (the last 30 min, for
alerts) and in history.json (5-minute averages for the last 7 days, for the charts).
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

STEP_S = 300                      # one history point per 5 minutes
KEEP = timedelta(days=7)
RECENT = timedelta(minutes=30)    # the longest alert window
SERIES = ("cpu_pct", "mem_used_gib", "disk_pct", "qwen_tps", "qwen_running", "qwen_waiting", "qwen_cache_pct")


# ── Qwen (vLLM or SGLang /metrics, Prometheus text format) ────────────
def parse_prometheus(text: str) -> dict[str, float]:
    """Metric name -> value, summed over label sets. Comments and unparsable lines are skipped."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            name, rest = line[:line.index("{")], line[line.rindex("}") + 1:]
        else:
            name, _, rest = line.partition(" ")
        try:
            out[name] = out.get(name, 0.0) + float(rest.split()[0])
        except (IndexError, ValueError):
            continue
    return out


def _first(m: dict[str, float], *names: str) -> float | None:
    return next((m[n] for n in names if n in m), None)


def qwen_reading(text: str) -> dict:
    """The serving numbers we chart, from either vLLM's or SGLang's metric names."""
    m = parse_prometheus(text)
    cache = _first(m, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc", "sglang:token_usage")
    return {"tokens_total": _first(m, "vllm:generation_tokens_total", "sglang:generation_tokens_total"),
            "running": _first(m, "vllm:num_requests_running", "sglang:num_running_reqs"),
            "waiting": _first(m, "vllm:num_requests_waiting", "sglang:num_queue_reqs"),
            "cache_pct": round(cache * 100, 1) if cache is not None else None}


def metrics_url(base_url: str) -> str:
    """http://spark:8000/v1 -> http://spark:8000/metrics"""
    base = base_url.rstrip("/")
    return (base[:-3] if base.endswith("/v1") else base) + "/metrics"


# ── devbox ─────────────────────────────────────────────────────────────
class CpuMeter:
    """CPU busy % since the previous call, from /proc/stat (Linux). None elsewhere or on the first call."""

    def __init__(self, path: Path = Path("/proc/stat")):
        self.path, self.prev = path, None

    def read(self) -> float | None:
        try:
            fields = [float(x) for x in self.path.read_text().splitlines()[0].split()[1:]]
        except (OSError, ValueError, IndexError):
            return None
        idle, total = fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
        prev, self.prev = self.prev, (idle, total)
        if prev is None or total <= prev[1]:
            return None
        return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def memory(path: Path = Path("/proc/meminfo")) -> tuple[float | None, float | None]:
    """(used GiB, total GiB) from /proc/meminfo; (None, None) elsewhere."""
    try:
        kb = {line.split(":")[0]: float(line.split()[1]) for line in path.read_text().splitlines() if ":" in line}
        total, avail = kb["MemTotal"], kb["MemAvailable"]
    except (OSError, ValueError, KeyError, IndexError):
        return None, None
    return round((total - avail) / 2**20, 2), round(total / 2**20, 2)


def disk_pct(path: Path) -> float | None:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return round(100 * u.used / u.total, 1)


# ── sampling ───────────────────────────────────────────────────────────
class Monitor:
    """Takes a sample per live-updater tick and keeps the last RECENT of them for the alert rules."""

    def __init__(self, *, qwen_url: str | None = None, disk_path: Path | None = None, cpu: CpuMeter | None = None,
                 fetch=None):
        self.qwen_url, self.disk_path = qwen_url, disk_path
        self.cpu = cpu or CpuMeter()
        self.fetch = fetch or _fetch
        self.recent: deque[tuple[datetime, dict]] = deque()
        self.mem_total_gib: float | None = None
        self.qwen_down_since: datetime | None = None
        self._tokens: tuple[float, float] | None = None  # (monotonic s, generation_tokens_total)

    def sample(self, now: datetime) -> dict:
        used, total = memory()
        self.mem_total_gib = total or self.mem_total_gib
        s = {"cpu_pct": self.cpu.read(), "mem_used_gib": used, "disk_pct": disk_pct(self.disk_path)
             if self.disk_path else None}
        s |= self._qwen(now)
        self.add(now, s)
        return s

    def _qwen(self, now: datetime) -> dict:
        empty = {"qwen_tps": None, "qwen_running": None, "qwen_waiting": None, "qwen_cache_pct": None}
        if not self.qwen_url:
            return empty
        try:
            r = qwen_reading(self.fetch(self.qwen_url))
        except Exception:  # unreachable, timeout, bad response: all the same to the page
            self.qwen_down_since = self.qwen_down_since or now
            self._tokens = None
            return empty
        self.qwen_down_since = None
        tps = None
        if r["tokens_total"] is not None:
            t = time.monotonic()
            if self._tokens is not None and t > self._tokens[0] and r["tokens_total"] >= self._tokens[1]:
                tps = round((r["tokens_total"] - self._tokens[1]) / (t - self._tokens[0]), 1)
            self._tokens = (t, r["tokens_total"])
        return {"qwen_tps": tps, "qwen_running": r["running"], "qwen_waiting": r["waiting"],
                "qwen_cache_pct": r["cache_pct"]}

    def add(self, now: datetime, s: dict) -> None:
        self.recent.append((now, s))
        while self.recent and now - self.recent[0][0] > RECENT:
            self.recent.popleft()

    def latest(self) -> dict:
        return self.recent[-1][1] if self.recent else {}

    def average(self, key: str, window: timedelta, now: datetime) -> float | None:
        """Mean of `key` over the last `window`, or None unless samples cover (nearly) the whole window."""
        pts = [(t, s[key]) for t, s in self.recent if now - t <= window and s.get(key) is not None]
        if not pts or now - pts[0][0] < window * 0.9:
            return None
        return sum(v for _, v in pts) / len(pts)


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - fixed URL from the deployment config
        return r.read(2_000_000).decode("utf-8", "replace")


# ── history.json ───────────────────────────────────────────────────────
class History:
    """5-minute averages of each series for the last 7 days, columnar: {"step_s", "t": [...], "series": {k: [...]}}."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.t: list[int] = []
        self.series: dict[str, list] = {k: [] for k in SERIES}
        self.mem_total_gib: float | None = None
        self._bucket: int | None = None
        self._acc: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            d = json.loads(self.path.read_text())
            t, series = [int(x) for x in d["t"]], d["series"]
            if all(len(series.get(k, [])) == len(t) for k in SERIES):
                self.t, self.series = t, {k: list(series[k]) for k in SERIES}
        except (OSError, ValueError, KeyError, TypeError):
            pass  # start fresh: losing chart history is harmless

    def add(self, now: datetime, s: dict, *, mem_total_gib: float | None = None) -> bool:
        """Fold a sample into its 5-minute bucket. Returns True when a finished bucket was written out."""
        self.mem_total_gib = mem_total_gib or self.mem_total_gib
        bucket = int(now.timestamp()) // STEP_S * STEP_S
        flushed = False
        if self._bucket is not None and bucket != self._bucket:
            self._flush(now)
            flushed = True
        if self._bucket != bucket:
            self._bucket, self._acc = bucket, {}
        for k in SERIES:
            if s.get(k) is not None:
                self._acc.setdefault(k, []).append(float(s[k]))
        return flushed

    def _flush(self, now: datetime) -> None:
        if self.t and self.t[-1] >= self._bucket:
            return
        self.t.append(self._bucket)
        for k in SERIES:
            vals = self._acc.get(k)
            self.series[k].append(round(sum(vals) / len(vals), 2) if vals else None)
        cutoff = now.timestamp() - KEEP.total_seconds()
        drop = next((i for i, t in enumerate(self.t) if t >= cutoff), len(self.t))
        if drop:
            self.t = self.t[drop:]
            self.series = {k: v[drop:] for k, v in self.series.items()}
        self.write()

    def to_json(self) -> dict:
        return {"step_s": STEP_S, "mem_total_gib": self.mem_total_gib, "t": self.t, "series": self.series}

    def write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("." + self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), separators=(",", ":"), allow_nan=False))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)
