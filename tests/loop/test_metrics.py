import json
from datetime import UTC, datetime, timedelta

from loop import metrics

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

VLLM = """# HELP vllm:num_requests_running Number of requests running
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="qwen"} 7.0
vllm:num_requests_waiting{model_name="qwen"} 2.0
vllm:kv_cache_usage_perc{model_name="qwen"} 0.83
vllm:generation_tokens_total{model_name="qwen"} 1000.0
vllm:request_success_total{finished_reason="stop",model_name="qwen"} 5
vllm:request_success_total{finished_reason="length",model_name="qwen"} 3
garbage line
"""
SGLANG = "sglang:num_running_reqs 8\nsglang:num_queue_reqs 0\nsglang:token_usage 0.5\n" \
         "sglang:generation_tokens_total 42\n"


def test_parse_prometheus_sums_label_sets_and_skips_junk():
    m = metrics.parse_prometheus(VLLM)
    assert m["vllm:request_success_total"] == 8 and m["vllm:num_requests_running"] == 7
    assert "garbage" not in m


def test_qwen_reading_vllm_and_sglang():
    assert metrics.qwen_reading(VLLM) == {"tokens_total": 1000.0, "running": 7.0, "waiting": 2.0, "cache_pct": 83.0}
    assert metrics.qwen_reading(SGLANG) == {"tokens_total": 42.0, "running": 8.0, "waiting": 0.0, "cache_pct": 50.0}


def test_metrics_url():
    assert metrics.metrics_url("http://spark:8000/v1") == "http://spark:8000/metrics"
    assert metrics.metrics_url("http://spark:8000/v1/") == "http://spark:8000/metrics"
    assert metrics.metrics_url("http://spark:30000") == "http://spark:30000/metrics"


def test_cpu_meter_reads_busy_share_between_calls(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    cpu = metrics.CpuMeter(stat)
    assert cpu.read() is None
    stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")  # +100 busy, +100 idle
    assert cpu.read() == 50.0
    assert metrics.CpuMeter(tmp_path / "missing").read() is None


def test_memory_from_meminfo(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       26214400 kB\nMemFree: 1 kB\nMemAvailable:    7340032 kB\n")
    assert metrics.memory(p) == (18.0, 25.0)
    assert metrics.memory(tmp_path / "nope") == (None, None)


def test_monitor_tokens_per_second_and_down_tracking(monkeypatch):
    texts = iter([VLLM, VLLM.replace("1000.0", "1700.0"), RuntimeError, RuntimeError])
    clock = iter([0.0, 5.0])

    def fetch(url):
        t = next(texts)
        if t is RuntimeError:
            raise OSError("refused")
        return t

    monkeypatch.setattr(metrics.time, "monotonic", lambda: next(clock))
    m = metrics.Monitor(qwen_url="http://x/metrics", fetch=fetch, cpu=metrics.CpuMeter(metrics.Path("/nonexistent")))
    assert m.sample(T0)["qwen_tps"] is None
    assert m.sample(T0 + timedelta(seconds=5))["qwen_tps"] == 140.0
    s = m.sample(T0 + timedelta(seconds=20))
    assert s["qwen_tps"] is None and m.qwen_down_since == T0 + timedelta(seconds=20)
    m.sample(T0 + timedelta(seconds=35))
    assert m.qwen_down_since == T0 + timedelta(seconds=20)


def test_monitor_average_needs_a_covered_window():
    m = metrics.Monitor(cpu=metrics.CpuMeter(metrics.Path("/nonexistent")))
    for i in range(0, 10):
        m.add(T0 + timedelta(minutes=i), {"cpu_pct": 40.0})
    assert m.average("cpu_pct", timedelta(minutes=30), T0 + timedelta(minutes=9)) is None
    for i in range(10, 31):
        m.add(T0 + timedelta(minutes=i), {"cpu_pct": 40.0})
    assert m.average("cpu_pct", timedelta(minutes=30), T0 + timedelta(minutes=30)) == 40.0
    assert all(T0 + timedelta(minutes=30) - t <= metrics.RECENT for t, _ in m.recent)


def test_history_averages_buckets_trims_to_a_week_and_survives_restart(tmp_path):
    path = tmp_path / "history.json"
    h = metrics.History(path)
    assert not h.add(T0, {"cpu_pct": 20.0})
    assert not h.add(T0 + timedelta(minutes=2), {"cpu_pct": 40.0, "qwen_tps": 100.0})
    assert h.add(T0 + timedelta(minutes=5), {"cpu_pct": 90.0}, mem_total_gib=25.0)  # closes the 12:00 bucket
    d = json.loads(path.read_text())
    assert d["t"] == [int(T0.timestamp())] and d["series"]["cpu_pct"] == [30.0]
    assert d["series"]["qwen_tps"] == [100.0] and d["series"]["disk_pct"] == [None] and d["mem_total_gib"] == 25.0

    again = metrics.History(path)
    assert again.t == d["t"]
    later = T0 + timedelta(days=8)
    again.add(later, {"cpu_pct": 1.0})
    again.add(later + timedelta(minutes=5), {"cpu_pct": 1.0})
    assert again.t == [int(later.timestamp()) // 300 * 300]


def test_history_ignores_a_corrupt_file(tmp_path):
    p = tmp_path / "history.json"
    p.write_text("{not json")
    assert metrics.History(p).t == []
