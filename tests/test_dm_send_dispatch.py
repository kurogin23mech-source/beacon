"""Windows-path parity for the DM verb unification (ms-120 / e-3899).

`beacon dm send` is the canonical DM verb (delegating to `bus send --channel
dm`) and `dm audit` is the canonical read verb (was `dm log`, which collided
with the write-verb `beacon log`). Both must exist on the Python dispatch path
(beacon_cli/dispatch.py) too — otherwise Windows pipx users hit argparse
`invalid choice: 'send'` / `'audit'`, the exact 2026-06-07 cross-machine DM
breakage class the dispatch-parity lint was created for.

Strategy mirrors tests/test_ms55_dispatch.py: patch subprocess.call inside
beacon_cli.dispatch to capture the (cmd, env) that would have reached
commands.py, then assert each argv path produces the right env contract.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch, main as main_mod  # noqa: E402


@pytest.fixture
def captured_call(monkeypatch):
    box: Dict[str, Any] = {"cmd": None, "env": None}

    def fake_call(cmd, env=None, **kwargs):
        box["cmd"] = list(cmd)
        box["env"] = dict(env) if env is not None else None
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    return box


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": []}'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


# ---------------------------------------------------------------------------
# dm send — canonical DM verb, delegates to bus send --channel dm
# ---------------------------------------------------------------------------

def test_dm_send_forces_dm_channel_and_forwards_recipient(
        project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "dm", "send", "--to", "sess-x", "--payload", '{"text":"hi"}',
    ])
    assert rc == 0
    env = captured_call["env"]
    # Delegated to the bus send handler with the channel forced to dm.
    assert env["BEACON_BUS_CHANNEL"] == "dm"
    assert env["BEACON_BUS_RECIPIENT_SESSION"] == "sess-x"
    assert env["BEACON_BUS_PAYLOAD"] == '{"text":"hi"}'
    assert captured_call["cmd"][-1] == "bus_send"


def test_dm_send_threads_manual_override(project_dir, no_bash, captured_call):
    """e-3901's --manual escape must be reachable via `dm send` on Windows too."""
    rc = main_mod.main([
        "dm", "send", "--to", "sess-x", "--payload", "{}", "--manual",
    ])
    assert rc == 0
    assert captured_call["env"]["BEACON_BUS_MANUAL"] == "1"


def test_bus_send_threads_manual_override(project_dir, no_bash, captured_call):
    """Parity: `bus send --manual` also carries the flag (e-3901 gap closed)."""
    rc = main_mod.main([
        "bus", "send", "--channel", "dm", "--to", "sess-x",
        "--payload", "{}", "--manual",
    ])
    assert rc == 0
    assert captured_call["env"]["BEACON_BUS_MANUAL"] == "1"


def test_bus_send_without_manual_is_empty(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "send", "--channel", "dm", "--to", "sess-x", "--payload", "{}",
    ])
    assert rc == 0
    assert captured_call["env"]["BEACON_BUS_MANUAL"] == ""


# ---------------------------------------------------------------------------
# dm audit (canonical) + dm log (deprecated alias)
# ---------------------------------------------------------------------------

def test_dm_audit_forwards_to_dm_log_command(project_dir, no_bash, captured_call):
    rc = main_mod.main(["dm", "audit", "--limit", "10"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "dm_log"
    assert captured_call["env"]["BEACON_DM_LOG_LIMIT"] == "10"


def test_dm_log_alias_still_works_and_warns(
        project_dir, no_bash, captured_call, capsys):
    rc = main_mod.main(["dm", "log", "--limit", "5"])
    assert rc == 0
    # Same underlying command as audit.
    assert captured_call["cmd"][-1] == "dm_log"
    assert captured_call["env"]["BEACON_DM_LOG_LIMIT"] == "5"
    # But nudges toward the canonical name.
    err = capsys.readouterr().err
    assert "dm audit" in err


def test_dm_send_and_audit_parse_without_invalid_choice(project_dir):
    """The core Windows-parity guarantee: argparse accepts both new subverbs."""
    parser = dispatch.build_parser()
    for argv in (["dm", "send", "--to", "x", "--payload", "{}"],
                 ["dm", "audit"],
                 ["dm", "log"]):
        ns = parser.parse_args(argv)  # must not raise SystemExit
        assert ns.dm_cmd == argv[1]


# ---------------------------------------------------------------------------
# bus listen recovery hint helper (ms-120 / e-3899, part 2)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(ROOT / "lib"))
import commands  # noqa: E402


def test_channel_missing_reason_no_mcp_json(tmp_path):
    assert ".mcp.json" in (commands._bus_channel_missing_reason(str(tmp_path)) or "")


def test_channel_missing_reason_no_beacon_bus_entry(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"other": {}}}')
    reason = commands._bus_channel_missing_reason(str(tmp_path))
    assert reason and "beacon-bus" in reason


def test_channel_missing_reason_malformed(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    reason = commands._bus_channel_missing_reason(str(tmp_path))
    assert reason and "読めません" in reason


def test_channel_present_returns_none(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"beacon-bus": {}}}')
    assert commands._bus_channel_missing_reason(str(tmp_path)) is None
