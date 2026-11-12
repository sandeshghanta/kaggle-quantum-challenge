import shutil

import pytest

from loop.pi_runner import run_pi, write_agent_dir
from tests.fakes.openai_server import FakeOpenAI

pytestmark = [pytest.mark.pi, pytest.mark.skipif(shutil.which("pi") is None, reason="pi CLI not installed")]


def setup(tmp_path, base_url):
    work = tmp_path / "work"
    work.mkdir()
    (work / "gen.py").write_text("x = 'old'\n")
    agent = write_agent_dir(tmp_path / "agent", base_url=base_url, model="fake-model", api_key_env="FAKE_KEY",
                            context_window=32768, max_tokens=1024)
    sysfile = tmp_path / "sys.md"
    sysfile.write_text("Be brief.")
    return work, agent, sysfile


def test_pi_edits_file_and_reports_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    script = [{"tool_calls": [{"name": "edit", "arguments": {"path": "gen.py",
                                                              "edits": [{"oldText": "old", "newText": "new"}]}}]},
              {"text": "SUMMARY: changed x"}]
    with FakeOpenAI(script) as fake:
        work, agent, sysfile = setup(tmp_path, fake.base_url)
        r = run_pi(work, agent, prompt="Improve it.", system_append=sysfile, tools=["read", "edit"],
                   model="fake-model", timeout_s=60)
    assert r.ok, r.error
    assert (work / "gen.py").read_text() == "x = 'new'\n"
    assert r.final_text.startswith("SUMMARY:") and r.tokens_out > 0


def test_pi_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    with FakeOpenAI([{"text": "late"}], delay=30) as fake:
        work, agent, sysfile = setup(tmp_path, fake.base_url)
        r = run_pi(work, agent, prompt="hi", system_append=sysfile, tools=[], model="fake-model", timeout_s=2)
    assert not r.ok and r.error == "timeout"


def test_usage_is_summed_over_turns(tmp_path, monkeypatch):  # review I6
    monkeypatch.setenv("FAKE_KEY", "k")
    script = [{"tool_calls": [{"name": "read", "arguments": {"path": "gen.py"}}]}, {"text": "SUMMARY: looked"}]
    with FakeOpenAI(script) as fake:
        work, agent, sysfile = setup(tmp_path, fake.base_url)
        r = run_pi(work, agent, prompt="Look.", system_append=sysfile, tools=["read"], model="fake-model",
                   timeout_s=60)
    assert r.ok and r.tokens_out == 2 * 20 and r.tokens_in == 2 * 100


def test_on_event_sees_parsed_events(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    events = []
    with FakeOpenAI([{"text": "SUMMARY: hi"}]) as fake:
        work, agent, sysfile = setup(tmp_path, fake.base_url)
        r = run_pi(work, agent, prompt="hi", system_append=sysfile, tools=[], model="fake-model", timeout_s=60,
                   on_event=events.append)
    assert r.ok and events and all(isinstance(e, dict) for e in events)
    assert any(e.get("type") == "message_end" for e in events)


def test_output_token_budget_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "k")
    script = [{"tool_calls": [{"name": "read", "arguments": {"path": "gen.py"}}]},
              {"tool_calls": [{"name": "read", "arguments": {"path": "gen.py"}}]}, {"text": "SUMMARY: never"}]
    with FakeOpenAI(script) as fake:
        work, agent, sysfile = setup(tmp_path, fake.base_url)
        r = run_pi(work, agent, prompt="Look.", system_append=sysfile, tools=["read"], model="fake-model",
                   timeout_s=60, max_output_tokens=20)
    assert not r.ok and r.error == "token budget" and r.tokens_out == 20


def test_check_extension_runs_the_sample_check_and_enforces_its_limit(tmp_path, monkeypatch):
    import sys
    from loop import config
    monkeypatch.setenv("FAKE_KEY", "k")
    ext = config.REPO / "roles" / "lab_assistant" / "v2" / "tools.ts"
    script = [{"tool_calls": [{"name": "check", "arguments": {}}]},
              {"tool_calls": [{"name": "plan", "arguments": {"idea": "break it"}}]},
              {"tool_calls": [{"name": "check", "arguments": {}}]},
              {"tool_calls": [{"name": "check", "arguments": {}}]}, {"text": "SUMMARY: checked"}]
    events = []
    with FakeOpenAI(script) as fake:
        work, agent, sysfile = setup(tmp_path, fake.base_url)
        (work / "gen.py").write_text("raise RuntimeError('boom in gen')\n")
        r = run_pi(work, agent, prompt="Check.", system_append=sysfile, tools=["read", "plan", "check"], model="fake-model",
                   timeout_s=120, extensions=[ext], extra_ro=[config.REPO],
                   extra_env={"LOOP_PYTHON": sys.executable, "LOOP_REPO": str(config.REPO), "LOOP_CHECK_MAX": "1"},
                   on_event=events.append)
    assert r.ok, r.error
    ends = [e for e in events if e.get("type") == "tool_execution_end" and e.get("toolName") == "check"]
    texts = [c["text"] for e in ends for c in e["result"]["content"]]
    assert len(ends) == 3 and "before running `check`" in texts[0]
    assert "BUILD FAILED" in texts[1] and "boom in gen" in texts[1] and "No checks left" in texts[2]
    starts = [e for e in events if e.get("type") == "tool_execution_start" and e.get("toolName") == "plan"]
    assert starts[0]["args"] == {"idea": "break it"}
