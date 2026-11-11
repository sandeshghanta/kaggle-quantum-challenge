import json
import subprocess
import sys

from substrate.cli import Substrate


def run(root, *args):
    return subprocess.run([sys.executable, "-m", "substrate.cli", "--root", str(root), *args],
                          capture_output=True, text=True)


def test_init_registers_every_schema_once(tmp_path):
    s = Substrate(tmp_path)
    assert s.init().registered == 15 and s.init().registered == 0
    assert len(s.history("schema:direction")) == 1


def test_cli_append_and_history_roundtrip(tmp_path):
    r = run(tmp_path, "append", "--type", "question.asked", "--subject", "bench:invsqrt",
            "--actor", '{"role":"t"}', "--payload", '{"question":"q"}')
    assert r.returncode == 0 and len(json.loads(r.stdout)["id"]) == 26
    h = json.loads(run(tmp_path, "history", "bench:invsqrt").stdout)
    assert h[0]["payload"] == {"question": "q"} and h[0]["seq"] == 1


def test_cli_rejects_bad_payload_and_bad_json(tmp_path):
    r = run(tmp_path, "append", "--type", "question.asked", "--subject", "bench:x",
            "--actor", "{}", "--payload", "{}")
    assert r.returncode == 2 and json.loads(r.stderr)["error"] == "SchemaViolation"
    r = run(tmp_path, "append", "--type", "question.asked", "--subject", "bench:x",
            "--actor", "{}", "--payload", "{not json")
    assert r.returncode == 2 and json.loads(r.stderr)["error"] == "ValueError"
    assert json.loads(run(tmp_path, "since").stdout) == {"events": [], "cursor": 0}


def test_blob_roundtrip_via_cli(tmp_path):
    f = tmp_path / "in.bin"
    f.write_bytes(b"abc")
    d = json.loads(run(tmp_path, "put-blob", str(f)).stdout)["digest"]
    out = tmp_path / "out.bin"
    assert run(tmp_path, "get-blob", d, "--out", str(out)).returncode == 0 and out.read_bytes() == b"abc"


def test_missing_blob_via_cli_is_a_clean_error(tmp_path):
    r = run(tmp_path, "get-blob", "sha256:" + "0" * 64, "--out", str(tmp_path / "o"))
    assert r.returncode == 2 and json.loads(r.stderr)["error"] == "BlobNotFound"


def test_export_writes_events_as_stored(tmp_path):
    s = Substrate(tmp_path)
    s.append("question.asked", {"question": "q"}, subject="bench:x", actor={})
    assert s.export(tmp_path / "e.jsonl").events == 1
    [line] = (tmp_path / "e.jsonl").read_text().splitlines()
    ev = json.loads(line)
    assert "seq" not in ev and ev["payload"] == {"question": "q"} and ev["v"] == 1


def test_query_commands_return_json(tmp_path):
    s = Substrate(tmp_path)
    assert json.loads(run(tmp_path, "frontier", "invsqrt").stdout) == []
    assert json.loads(run(tmp_path, "lineage", "sha256:" + "a" * 64).stdout) == []
    b = json.loads(run(tmp_path, "budget", "claude", "--window", "5h").stdout)
    assert b["pool"] == "claude" and b["events"] == 0
    assert s.frontier("invsqrt") == [] and s.lineage("sha256:" + "a" * 64) == []
