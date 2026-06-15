"""Win parity coverage for the ms-73 DM / bus CLI mirror (e-1763).

Until this MS, ``beacon bus send / listen / receive / ack`` were wired
to Python (ms-54 e-1151) but were missing four DM-routing /
envelope-issuance flags that the bash dispatcher already understood:

  * ``--to <recipient>``   → BEACON_BUS_RECIPIENT_SESSION
  * ``--action <name>...`` → BEACON_BUS_ACTION (comma-joined for envelope)
  * ``--no-envelope``      → BEACON_BUS_NO_ENVELOPE=1
  * ``--event <event_id>`` → BEACON_BUS_ACK_EVENT_ID (single-event ack)

Without these flags Windows pipx users could not send authorized
cross-user DMs nor ack a specific event the way bus.mjs / Skills
expect. These tests assert each argv path produces the same env shape
bin/beacon emits, so commands.py + bus.mjs observe an identical
contract regardless of which OS spawned the call.

Role split (= ms-73 e-1763 documented in dispatch.py docstring):
  bus.mjs  = transport + bridge poll (OS-independent, runs out-of-process)
  commands = operator surface (send / listen / receive / ack / status)
  dispatch = argv → env shim (this file's parser is what we exercise)
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
# bus send — DM routing + envelope issuance (e-1763)
# ---------------------------------------------------------------------------


def test_bus_send_minimal_path(project_dir, no_bash, captured_call):
    """Existing minimal path stays green after envelope flags landed."""
    rc = main_mod.main([
        "bus", "send",
        "--channel", "dm",
        "--payload", '{"text":"hi"}',
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_CHANNEL"] == "dm"
    assert env["BEACON_BUS_PAYLOAD"] == '{"text":"hi"}'
    # New flags default to empty / unset, matching bash behavior.
    assert env["BEACON_BUS_RECIPIENT_SESSION"] == ""
    assert env["BEACON_BUS_ACTION"] == ""
    assert env["BEACON_BUS_NO_ENVELOPE"] == ""
    assert captured_call["cmd"][-1] == "bus_send"


def test_bus_send_routes_to_recipient_session(
    project_dir, no_bash, captured_call
):
    """--to <sid> populates BEACON_BUS_RECIPIENT_SESSION (= cross-user DM)."""
    rc = main_mod.main([
        "bus", "send",
        "--channel", "dm",
        "--to", "sv-recipient-1234",
        "--payload", '{"text":"hello"}',
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_RECIPIENT_SESSION"] == "sv-recipient-1234"


def test_bus_send_collects_actions_comma_joined(
    project_dir, no_bash, captured_call
):
    """Repeated --action flags must be comma-joined (e-1290 envelope path)."""
    rc = main_mod.main([
        "bus", "send",
        "--channel", "dm",
        "--to", "sv-target",
        "--action", "search:doc",
        "--action", "read:profile",
        "--action", "send:summary",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_ACTION"] == "search:doc,read:profile,send:summary"


def test_bus_send_no_envelope_opt_out(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "send",
        "--channel", "dm",
        "--to", "sv-debug",
        "--no-envelope",
    ])
    assert rc == 0
    assert captured_call["env"]["BEACON_BUS_NO_ENVELOPE"] == "1"


def test_bus_send_in_reply_to_preserved(project_dir, no_bash, captured_call):
    """Reply-mode flag must still propagate after the new flags joined."""
    rc = main_mod.main([
        "bus", "send",
        "--channel", "dm",
        "--to", "sv-x",
        "--in-reply-to", "evt-abc-123",
        "--delivery", "propose-to-ai",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_IN_REPLY_TO"] == "evt-abc-123"
    assert env["BEACON_BUS_DELIVERY"] == "propose-to-ai"


def test_bus_send_project_override(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "send", "--channel", "dm",
        "--project", "beacon-other-project",
    ])
    assert rc == 0
    assert captured_call["env"]["BEACON_BUS_PROJECT_ID"] == "beacon-other-project"


# ---------------------------------------------------------------------------
# bus listen — block-read loop (e-1763 = non-regression after parser grew)
# ---------------------------------------------------------------------------


def test_bus_listen_minimal(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "listen", "--recipient", "sv-me", "--interval", "5",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_RECIPIENT"] == "sv-me"
    assert env["BEACON_BUS_INTERVAL"] == "5"
    assert captured_call["cmd"][-1] == "bus_listen"


def test_bus_listen_auto_ack_and_once(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "listen", "--recipient", "sv-me",
        "--auto-ack", "--once",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_AUTO_ACK"] == "1"
    assert env["BEACON_BUS_ONCE"] == "1"


# ---------------------------------------------------------------------------
# bus receive — one-shot block-read (e-1763 non-regression)
# ---------------------------------------------------------------------------


def test_bus_receive_with_timeout(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "receive", "--recipient", "sv-me",
        "--timeout", "30", "--auto-ack",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_RECIPIENT"] == "sv-me"
    assert env["BEACON_BUS_TIMEOUT"] == "30"
    assert env["BEACON_BUS_AUTO_ACK"] == "1"
    assert captured_call["cmd"][-1] == "bus_receive"


# ---------------------------------------------------------------------------
# bus ack — cursor advance + single-event ack (e-1763 = --event added)
# ---------------------------------------------------------------------------


def test_bus_ack_last_seen_at_only(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "ack",
        "--recipient", "sv-me",
        "--last-seen-at", "2026-06-15T10:00:00Z",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_RECIPIENT"] == "sv-me"
    assert env["BEACON_BUS_LAST_SEEN_AT"] == "2026-06-15T10:00:00Z"
    assert env["BEACON_BUS_ACK_EVENT_ID"] == ""
    assert captured_call["cmd"][-1] == "bus_ack"


def test_bus_ack_single_event_id(project_dir, no_bash, captured_call):
    """--event <id> for explicit single-event ack (e-1423)."""
    rc = main_mod.main([
        "bus", "ack",
        "--recipient", "sv-me",
        "--event", "evt-trigger-9876",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_BUS_ACK_EVENT_ID"] == "evt-trigger-9876"


def test_bus_ack_with_project_override(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "bus", "ack",
        "--recipient", "sv-me",
        "--last-seen-at", "2026-06-15T00:00:00Z",
        "--project", "beacon-target",
    ])
    assert rc == 0
    assert captured_call["env"]["BEACON_BUS_PROJECT_ID"] == "beacon-target"


# ---------------------------------------------------------------------------
# Help surface — new flags must appear so users can discover them
# ---------------------------------------------------------------------------


def test_bus_help_mentions_new_send_flags(project_dir, no_bash, capsys):
    rc = main_mod.main(["bus", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    for flag in ("--to", "--action", "--no-envelope", "--event"):
        assert flag in out, f"Help missing flag: {flag!r}"
