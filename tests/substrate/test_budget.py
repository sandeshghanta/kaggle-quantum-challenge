from datetime import UTC, datetime

import pytest
from conftest import spend

from substrate._projections import budget


def test_sums_within_window_only(st, monkeypatch):
    spend(st, "claude", "2026-11-11T09:59:00.000Z", monkeypatch, tokens_in=100, tokens_out=10, tokens_cached=50)
    spend(st, "claude", "2026-11-11T14:00:00.000Z", monkeypatch, tokens_in=1, tokens_out=1, usd=0.5)
    spend(st, "qwen", "2026-11-11T14:00:00.000Z", monkeypatch, tokens_in=999, tokens_out=999)
    b = budget(st, "claude", window="5h", now=datetime(2026, 11, 11, 15, tzinfo=UTC))
    assert (b.tokens_in, b.tokens_out, b.tokens_cached, b.usd, b.events) == (1, 1, 0, 0.5, 1)
    assert b.pool == "claude" and b.window == "5h" and b.cpu_seconds == 0


def test_null_fields_count_as_zero(st, monkeypatch):
    spend(st, "cpu", "2026-11-11T14:00:00.000Z", monkeypatch, cpu_seconds=12.5, usd=None)
    b = budget(st, "cpu", window="1d", now=datetime(2026, 11, 11, 15, tzinfo=UTC))
    assert b.cpu_seconds == 12.5 and b.usd == 0 and b.events == 1


def test_bad_window_rejected(st):
    with pytest.raises(ValueError):
        budget(st, "claude", window="week")
