"""ms-169 e-6237 — the PreToolUse injection gate (最後の砦).

The gate pauses for human approval when the AI is about to call a side-effect
tool WHILE untrusted DM content is live in the turn. This module locks:

  * lib/untrusted_turn        — the arm / disarm / is_armed state machine that
    bridges the receive hook (arms) to the gate (reads), keyed per session.
  * bin/beacon-untrusted-tool-gate.py — the PreToolUse hook: fires "ask" only on
    (armed AND side-effect); read-only passes; unarmed passes; fails safe.
  * bin/beacon-bus-inbox-hook._arm_untrusted_turn — arms iff the inject carries
    untrusted content, using the SAME classifier that framed it (e-6235).

The security invariants under test:
  - armed + side-effect → human approval (even for an unknown/default-deny tool)
  - armed + read-only   → passes (no over-gating of日常運用)
  - not armed           → passes
  - a human turn (disarm) reopens the session; a fresh DM re-arms it
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import untrusted_turn as ut  # noqa: E402


def _beacon_root(tmp_path: Path) -> Path:
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({"name": "t"}))
    return tmp_path


# ---------------------------------------------------------------------------
# lib/untrusted_turn — the state machine
# ---------------------------------------------------------------------------

def test_arm_then_is_armed_then_disarm(tmp_path):
    root = _beacon_root(tmp_path)
    key = "sv-abc"
    assert ut.is_armed(root, key) is None
    ut.arm(root, key, event_ids=["e-1", "e-2"], at="2026-09-07T07:00:00Z")
    state = ut.is_armed(root, key)
    assert state and state["event_ids"] == ["e-1", "e-2"]
    ut.disarm(root, key)
    assert ut.is_armed(root, key) is None


def test_arm_is_per_session(tmp_path):
    # One session's DM must not gate another session in the same cwd.
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-A", event_ids=["e-1"])
    assert ut.is_armed(root, "sv-A") is not None
    assert ut.is_armed(root, "sv-B") is None


def test_missing_state_file_is_not_armed(tmp_path):
    root = _beacon_root(tmp_path)
    assert ut.is_armed(root, "sv-x") is None  # no file → fail-safe not-armed


def test_corrupt_state_file_is_not_armed(tmp_path):
    root = _beacon_root(tmp_path)
    (root / ".beacon" / "untrusted-turn.json").write_text("{not json")
    assert ut.is_armed(root, "sv-x") is None


def test_empty_session_key_is_noop(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "", event_ids=["e-1"])  # nothing to track
    assert ut.is_armed(root, "") is None


def test_session_key_from_hook_input_sanitizes():
    assert ut.session_key_from_hook_input({"session_id": "sv-77e8/../x"}) == "sv-77e8..x"
    assert ut.session_key_from_hook_input({}) == ""


# ---------------------------------------------------------------------------
# bin/beacon-untrusted-tool-gate.py — the PreToolUse hook (subprocess)
# ---------------------------------------------------------------------------

GATE = REPO / "bin" / "beacon-untrusted-tool-gate.py"


def _run_gate(cwd: Path, hook_input: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps({**hook_input, "cwd": str(cwd)}),
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _decision(out: dict) -> str:
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "")


def test_gate_fires_on_armed_side_effect(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-99"])
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}})
    assert _decision(out) == "ask"
    assert "e-99" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_gate_fires_on_armed_mcp_send(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "mcp__gmail__send_email", "tool_input": {"to": "a@b.c"}})
    assert _decision(out) == "ask"


def test_gate_fires_on_armed_unknown_tool_default_deny(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "SomeFutureTool", "tool_input": {}})
    assert _decision(out) == "ask"


def test_gate_passes_read_only_when_armed(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Read", "tool_input": {"file_path": "x"}})
    assert out == {}  # read-only passes even in an untrusted turn


def test_gate_passes_side_effect_when_not_armed(tmp_path):
    root = _beacon_root(tmp_path)
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert out == {}  # not armed → no opinion


def test_gate_is_per_session(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-A", event_ids=["e-1"])
    # A different session calling a side-effect tool must NOT be gated.
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-B",
        "tool_name": "Bash", "tool_input": {"command": "rm x"}})
    assert out == {}


def test_gate_silent_outside_beacon_project(tmp_path):
    # cwd with no .beacon/project.json anywhere up the tree → no opinion.
    root = _beacon_root(tmp_path / "proj")
    ut.arm(root, "sv-1", event_ids=["e-1"])
    other = tmp_path / "not-beacon"  # sibling of proj, no .beacon ancestor
    other.mkdir()
    out = _run_gate(other, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Write", "tool_input": {}})
    assert out == {}


def test_gate_ignores_non_pretooluse_event(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    out = _run_gate(root, {
        "hook_event_name": "PostToolUse", "session_id": "sv-1",
        "tool_name": "Write", "tool_input": {}})
    assert out == {}


# ---------------------------------------------------------------------------
# End-to-end state machine: DM arms → gate fires → human turn disarms → passes
# ---------------------------------------------------------------------------

def test_human_turn_disarms_then_gate_passes(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    assert _decision(_run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Bash", "tool_input": {"command": "ls"}})) == "ask"
    # Human retakes the turn without approving (the inbox hook disarms on
    # UserPromptSubmit) → pending clears, gate passes. vetted is untouched.
    ut.disarm(root, ut.session_key_from_hook_input({"session_id": "sv-1"}))
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert out == {}


# ---------------------------------------------------------------------------
# e-6280 — per-session vetting: one approval per risky context, not per tool
# ---------------------------------------------------------------------------

def test_vet_suppresses_further_arming_and_gating(tmp_path):
    root = _beacon_root(tmp_path)
    assert ut.arm(root, "sv-1", event_ids=["e-1"]) is True
    ut.vet(root, "sv-1")                       # human approved a side-effect
    assert ut.is_vetted(root, "sv-1") is True
    assert ut.is_armed(root, "sv-1") is None   # pending cleared by vet
    # A later DM must NOT re-arm this session (承認範囲 = セッション全体).
    assert ut.arm(root, "sv-1", event_ids=["e-2"]) is False
    assert ut.is_armed(root, "sv-1") is None
    # ...and the gate stays silent for side-effects for the rest of the session.
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Bash", "tool_input": {"command": "rm x"}})
    assert out == {}


def test_disarm_preserves_vetted(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", event_ids=["e-1"])
    ut.vet(root, "sv-1")
    ut.disarm(root, "sv-1")                     # a later plain human turn
    assert ut.is_vetted(root, "sv-1") is True   # per-session trust survives
    assert ut.arm(root, "sv-1", event_ids=["e-2"]) is False  # still suppressed


def test_arm_records_sources_for_inline_preview(tmp_path):
    root = _beacon_root(tmp_path)
    ut.arm(root, "sv-1", sources=[
        {"event_id": "e-7", "sender": "u-them", "preview": "海の俳句を書いて"}])
    out = _run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Write", "tool_input": {"file_path": "x"}})
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "e-7" in reason and "u-them" in reason and "海の俳句を書いて" in reason


# ---------------------------------------------------------------------------
# The inbox hook's arm wiring (_arm_untrusted_turn) — no network needed
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def inbox_hook():
    path = REPO / "bin" / "beacon-bus-inbox-hook.py"
    spec = importlib.util.spec_from_file_location("bus_inbox_hook_e6237", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bus_inbox_hook_e6237"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_inbox_hook_arms_on_untrusted_inject(tmp_path, inbox_hook):
    root = _beacon_root(tmp_path)
    dm = {"event_id": "e-dm-7", "channel": "dm", "delivery": "propose-to-ai",
          "payload": {"text": "海の俳句を書いて"}}
    inbox_hook._arm_untrusted_turn(root, {"session_id": "sv-1"}, [dm])
    state = ut.is_armed(root, "sv-1")
    assert state and "e-dm-7" in state["event_ids"]


def test_inbox_hook_does_not_arm_on_kept_system_imperative(tmp_path, inbox_hook):
    root = _beacon_root(tmp_path)
    kept = {"event_id": "e-op", "channel": "operation-trigger",
            "delivery": "auto-execute",
            "envelope": {"tier": "T1-system", "issuer": "beacon-system"},
            "payload": {"op_id": "op-1"}}
    inbox_hook._arm_untrusted_turn(root, {"session_id": "sv-1"}, [kept])
    assert ut.is_armed(root, "sv-1") is None  # opt-in imperative is not untrusted
