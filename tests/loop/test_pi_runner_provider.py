"""run_pi's provider/sandbox switches, checked on the command line (no pi needed)."""

import pytest

from loop import pi_runner, sandbox


class Stop(Exception):
    pass


@pytest.fixture
def captured(monkeypatch, tmp_path):
    seen = {}

    def popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        raise Stop

    monkeypatch.setattr(pi_runner.subprocess, "Popen", popen)
    def wrap(cmd, **kw):
        seen["wrap"] = kw
        return ["SANDBOX", *cmd]

    monkeypatch.setattr(sandbox, "wrap", wrap)
    (tmp_path / "sys.md").write_text("x")
    return seen


def call(tmp_path, **kw):
    with pytest.raises(Stop):
        pi_runner.run_pi(tmp_path, tmp_path / "agent", prompt="hi", system_append=tmp_path / "sys.md", tools=[],
                         model="claude-opus-5-5", timeout_s=5, **kw)


def test_default_is_qwen_and_sandboxed(captured, tmp_path):
    call(tmp_path)
    cmd = captured["cmd"]
    assert cmd[0] == "SANDBOX" and cmd[cmd.index("--provider") + 1] == "qwen"
    assert cmd[cmd.index("--model") + 1] == "qwen/claude-opus-5-5" and "-nt" in cmd


def test_researcher_provider_unsandboxed(captured, tmp_path):
    call(tmp_path, provider="claude-bridge", sandboxed=False)
    cmd = captured["cmd"]
    assert cmd[0] == "pi" and cmd[cmd.index("--model") + 1] == "claude-bridge/claude-opus-5-5"
    assert captured["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path / "agent")


def test_extensions_tools_env_and_read_only_paths(captured, tmp_path):
    ext = tmp_path / "check.ts"
    ext.write_text("export default () => {}")
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(Stop):
        pi_runner.run_pi(tmp_path, tmp_path / "agent", prompt="hi", system_append=tmp_path / "sys.md",
                         tools=["read", "edit", "check"], model="m", timeout_s=5, extensions=[ext],
                         extra_ro=[repo], extra_env={"LOOP_CHECK_MAX": "8"})
    cmd = captured["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "read,edit,check" and cmd[cmd.index("-e") + 1] == str(ext)
    assert captured["env"]["LOOP_CHECK_MAX"] == "8"
    assert ext.resolve() in captured["wrap"]["ro"] and repo.resolve() in captured["wrap"]["ro"]


def test_agent_dir_provider(tmp_path):
    import json
    d = pi_runner.write_agent_dir(tmp_path / "a", base_url="http://x/v1", model="m", api_key_env="K",
                                  context_window=1, max_tokens=1, provider="claude-bridge")
    assert list(json.loads((d / "models.json").read_text())["providers"]) == ["claude-bridge"]
