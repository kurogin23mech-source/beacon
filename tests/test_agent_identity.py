"""Unit tests for ``lib/agent.py`` — actor identification (ms-51 / e-931).

These tests lock in the contract that other ms-51 features depend on:

* hostname fallback when no agent.json is present
* agent.json ``{"name"}`` override
* malformed agent.json silently degrades to hostname
* ``BEACON_AGENT_PARENT`` produces ``<parent>.dispatch-<n>`` suffixing
* ``BEACON_AGENT_CHILD_ID`` overrides the auto-generated suffix
* :func:`get_actor` returns a dict with both ``machine`` and ``agent``

We deliberately do **not** test the exact hostname value (it differs per
CI box); we only assert that it is non-empty and that the precedence
order holds when the agent name *should* be the hostname.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import agent  # noqa: E402


@pytest.fixture
def project_dir(monkeypatch):
    """Set CWD to a temp dir containing an empty .beacon/ folder.

    Most agent.py paths resolve relative to CWD, so we change CWD instead
    of mocking individual path helpers — that mirrors real CLI usage.
    """
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        monkeypatch.chdir(tmp)
        # Clean any inherited sub-agent env so each test starts fresh.
        monkeypatch.delenv("BEACON_AGENT_PARENT", raising=False)
        monkeypatch.delenv("BEACON_AGENT_CHILD_ID", raising=False)
        yield Path(tmp)


def _write_agent_json(project_dir: Path, payload):
    p = project_dir / ".beacon" / "agent.json"
    p.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# get_machine()
# ---------------------------------------------------------------------------

def test_get_machine_returns_hostname(project_dir):
    """machine is always socket.gethostname() — there is no override (yet)."""
    assert agent.get_machine() == socket.gethostname() or agent.get_machine() == "unknown"


# ---------------------------------------------------------------------------
# read_agent_config()
# ---------------------------------------------------------------------------

def test_no_agent_json_returns_empty_dict(project_dir):
    assert agent.read_agent_config() == {}


def test_valid_agent_json_is_parsed(project_dir):
    _write_agent_json(project_dir, {"name": "mac-claude"})
    assert agent.read_agent_config() == {"name": "mac-claude"}


def test_malformed_agent_json_degrades_to_empty(project_dir):
    (project_dir / ".beacon" / "agent.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    # Must NOT raise — a corrupted optional file must not break the CLI.
    assert agent.read_agent_config() == {}


def test_non_object_agent_json_is_ignored(project_dir):
    # JSON valid but not an object → treated as missing.
    (project_dir / ".beacon" / "agent.json").write_text("[1,2,3]", encoding="utf-8")
    assert agent.read_agent_config() == {}


# ---------------------------------------------------------------------------
# get_agent() — agent name with sub-agent suffixing
# ---------------------------------------------------------------------------

def test_get_agent_falls_back_to_hostname(project_dir):
    """No agent.json + no parent env → hostname (AC-5 fallback)."""
    assert agent.get_agent() == socket.gethostname() or agent.get_agent() == "unknown"


def test_get_agent_uses_agent_json_name(project_dir):
    _write_agent_json(project_dir, {"name": "mac-claude"})
    assert agent.get_agent() == "mac-claude"


def test_agent_json_name_trimmed(project_dir):
    _write_agent_json(project_dir, {"name": "  mac-claude  "})
    assert agent.get_agent() == "mac-claude"


def test_empty_agent_json_name_falls_back_to_hostname(project_dir):
    _write_agent_json(project_dir, {"name": "   "})
    assert agent.get_agent() == socket.gethostname() or agent.get_agent() == "unknown"


def test_subagent_parent_env_produces_dispatch_suffix(project_dir, monkeypatch):
    """BEACON_AGENT_PARENT alone → <parent>.dispatch-<pid> (AC-6)."""
    _write_agent_json(project_dir, {"name": "mac-claude"})
    monkeypatch.setenv("BEACON_AGENT_PARENT", "mac-claude")
    name = agent.get_agent()
    assert name.startswith("mac-claude.dispatch-")
    # Suffix must be deterministic: same PID → same suffix in this process.
    assert agent.get_agent() == name


def test_subagent_child_id_overrides_pid(project_dir, monkeypatch):
    """BEACON_AGENT_CHILD_ID gives the dispatcher control over the suffix."""
    monkeypatch.setenv("BEACON_AGENT_PARENT", "mac-claude")
    monkeypatch.setenv("BEACON_AGENT_CHILD_ID", "dispatch-1")
    assert agent.get_agent() == "mac-claude.dispatch-1"


def test_subagent_parent_overrides_local_agent_json(project_dir, monkeypatch):
    """When dispatched, parent identity wins over our own agent.json.

    Rationale: otherwise N sub-agents on the same box would all collide
    to the box's agent.json name and we'd lose dispatch tracking.
    """
    _write_agent_json(project_dir, {"name": "windows-claude"})
    monkeypatch.setenv("BEACON_AGENT_PARENT", "mac-claude")
    monkeypatch.setenv("BEACON_AGENT_CHILD_ID", "dispatch-2")
    assert agent.get_agent() == "mac-claude.dispatch-2"


def test_blank_parent_env_does_not_trigger_suffix(project_dir, monkeypatch):
    """Empty BEACON_AGENT_PARENT must NOT prepend a "." or activate suffix."""
    _write_agent_json(project_dir, {"name": "mac-claude"})
    monkeypatch.setenv("BEACON_AGENT_PARENT", "   ")
    assert agent.get_agent() == "mac-claude"


# ---------------------------------------------------------------------------
# get_actor() — the canonical helper
# ---------------------------------------------------------------------------

def test_get_actor_returns_machine_and_agent(project_dir):
    _write_agent_json(project_dir, {"name": "mac-claude"})
    actor = agent.get_actor()
    assert set(actor.keys()) == {"machine", "agent"}
    assert actor["agent"] == "mac-claude"
    assert actor["machine"]  # non-empty


def test_get_actor_with_subagent_shows_dispatch_chain(project_dir, monkeypatch):
    monkeypatch.setenv("BEACON_AGENT_PARENT", "mac-claude")
    monkeypatch.setenv("BEACON_AGENT_CHILD_ID", "dispatch-3")
    actor = agent.get_actor()
    assert actor["agent"] == "mac-claude.dispatch-3"
    assert actor["machine"]  # machine doesn't change for subagents
