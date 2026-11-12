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
