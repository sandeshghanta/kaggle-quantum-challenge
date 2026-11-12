import json

from loop.heartbeat import JobProgress, write_heartbeat


def test_write_is_atomic_and_complete(tmp_path):
    write_heartbeat(tmp_path / "st", 3, state="idle", last_outcome="committed")
    d = json.loads((tmp_path / "st" / "worker-3.json").read_text())
    assert d["worker"] == 3 and d["state"] == "idle" and d["last_outcome"] == "committed" and d["updated_at"]
    assert [p.name for p in (tmp_path / "st").iterdir()] == ["worker-3.json"]  # no tmp left behind


def test_none_status_dir_is_noop():
    write_heartbeat(None, 1, state="idle")


def test_unwritable_status_dir_never_raises(tmp_path):
    (tmp_path / "f").write_text("x")
    write_heartbeat(tmp_path / "f" / "sub", 1, state="idle")  # parent is a file


def test_progress_counts_turns_tools_files_tokens(tmp_path):
    p = JobProgress(tmp_path, 2, island="main", parent="sha256:abcdef0123456789", announcement="A1")
    p.on_event({"type": "tool_execution_start", "toolName": "edit", "args": {"path": "gen.py"}})
    p.on_event({"type": "tool_execution_start", "toolName": "read", "args": {"path": "other.py"}})
    p.on_event({"type": "message_end", "message": {"role": "assistant", "usage": {"output": 40}}})
    p.on_event({"type": "message_end", "message": {"role": "assistant", "usage": {"output": 25}}})
    p.on_event({"type": "message_end", "message": {"role": "user"}})
    d = json.loads((tmp_path / "worker-2.json").read_text())
    assert d["state"] == "thinking" and d["turns"] == 2 and d["last_tool"] == "read"
    assert d["files_edited"] == ["gen.py"] and d["tokens_out"] == 65
    assert d["island"] == "main" and d["parent"] == "sha256:abcdef0123456789" and d["announcement"] == "A1"


def test_progress_reads_tool_calls_inside_assistant_message(tmp_path):
    p = JobProgress(tmp_path, 1, island="i", parent="p", announcement="a")
    p.on_event({"type": "message_end", "message": {"role": "assistant", "content": [
        {"type": "toolCall", "name": "write", "arguments": {"path": "new.py"}}]}})
    d = json.loads((tmp_path / "worker-1.json").read_text())
    assert d["last_tool"] == "write" and d["files_edited"] == ["new.py"]


def test_finish_writes_idle_with_outcome(tmp_path):
    p = JobProgress(tmp_path, 1, island="i", parent="p", announcement="a")
    p.finish("failed", "timeout")
    d = json.loads((tmp_path / "worker-1.json").read_text())
    assert d["state"] == "idle" and d["last_outcome"] == "failed" and d["last_summary"] == "timeout" and d["finished_at"]
