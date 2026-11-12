"""Static guards on the NixOS module (no nix available in CI): the researcher unit must match the decisions."""

import re
from pathlib import Path

MODULE = Path("nix/module.nix").read_text()
VM = Path("nix/vm-test/configuration.nix").read_text()
RESEARCHER = MODULE[MODULE.index("research-loop-researcher-setup = {"):]


def test_researcher_unit_flags():
    for flag in ("--substrate ${target}", "--cap-pct", "--provider claude-bridge", "--agent-dir ${rhome}/pi-agent",
                 "--status-out ${statusDir}/researcher.json", "--interval-min"):
        assert flag in RESEARCHER
    assert 'default = "claude-opus-5-5"' in MODULE and "researcher.intervalMin" in MODULE and "default = 45" in MODULE
    assert 'user = "rl-researcher"' in RESEARCHER and "writable = [ rhome statusDir ]" in RESEARCHER


def test_status_dir_and_group():
    assert 'status-readers' in MODULE and 'status/researcher' in MODULE and "0750 rl-researcher status-readers" in MODULE
    assert "writable = [ rhome statusDir ]" in RESEARCHER


def test_no_anthropic_api_credentials_in_the_environment():
    assert not re.search(r"ANTHROPIC_(API_KEY|BASE_URL|AUTH_TOKEN)", MODULE)


def test_setup_installs_claude_code_and_bridge_and_target_wants_researcher():
    assert "npm install --prefix ${rhome}/npm @anthropic-ai/claude-code" in MODULE
    assert "pi install npm:pi-claude-bridge" in MODULE and "PI_CODING_AGENT_DIR" in MODULE
    assert 'systemd.targets.research-loop.wants = [ "research-loop-researcher.service" ]' in MODULE
    assert "0700 rl-researcher rl-researcher" in MODULE


def test_vm_config_disables_the_researcher():
    assert "researcher.enable = false" in VM
    assert "researcher.enable" in MODULE


def test_update_does_not_block_on_draining_units():
    upd = MODULE[MODULE.index('writeShellScriptBin "research-loop-update"'):MODULE.index("in {")]
    restarts = [l for l in upd.splitlines() if "systemctl restart" in l]
    assert restarts and all("--no-block" in l for l in restarts)


def test_verifier_stops_quickly():
    unit = MODULE[MODULE.index("systemd.services.research-loop-verifier = {"):]
    assert "TimeoutStopSec = 60;" in unit[:unit.index("script =")]


def test_live_updater_samples_hardware_and_writes_history():
    unit = MODULE[MODULE.index("systemd.services.research-loop-live = "):]
    unit = unit[:unit.index("systemd.services.research-loop-statuspage")]
    for flag in ("--history ${status}/live/history.json", "--qwen-base-url", "--disk-path ${cfg.dataDir}"):
        assert flag in unit


def test_worker_egress_allow_list():
    assert "model.egressAllow = lib.mkOption" in MODULE
    unit = MODULE[MODULE.index('systemd.services."research-loop-worker@" = {'):]
    unit = unit[:unit.index("script =")]
    assert 'IPAddressDeny = "any"' in unit and 'IPAddressAllow = [ "localhost" ] ++ cfg.model.egressAllow' in unit
    assert "model.egressAllow = [ ]" in VM
