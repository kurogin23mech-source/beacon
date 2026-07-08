"""Agent-kind-aware bridge-claim adoption (ms-93 / e-3091).

The bug: in a cwd where BOTH a Claude Code session and a Codex receive-loop
daemon run under the same terminal / pid tree, ``find_my_bridge_claim`` matched
bridge claims (``.beacon/bridges/<sid>.json``) by pid-tree ALONE. The claim
files carried only ``session_id`` / ``pid`` / ``parent_pid`` — no ``agent_kind``
— so a Claude Code caller happily adopted a Codex bridge's claim (and vice
versa) whenever they shared an ancestor pid. That made ``agent.kind`` on the
shared sid non-deterministic (last-writer-wins) and broke
``resolve_stable_identity(agent_kind="codex")`` (a codex- sid surfaced with
``agent.kind=claude-code``, observed 2026-07-08).

The fix keeps the intended sid-sharing anti-churn design but makes the resolver
agent_kind-aware: a claim that records ``agent_kind`` is only adopted by a
caller of the SAME kind; a legacy claim WITHOUT ``agent_kind`` stays adoptable
by anyone (pid-tree only). These tests pin both directions and the legacy
fallback.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session  # noqa: E402

MY_BCLAUDE_PID = 5000          # shared ancestor pid for BOTH daemons
ALIVE_CLAUDE_BRIDGE = 9001
ALIVE_CODEX_BRIDGE = 9002


def _write_claim(bridges_dir: Path, sid: str, *, pid: int, parent_pid: int,
                 agent_kind: str | None = None):
    bridges_dir.mkdir(parents=True, exist_ok=True)
    claim = {
        "session_id": sid, "pid": pid, "parent_pid": parent_pid,
        "cwd": str(bridges_dir.parent.parent), "started_at": "2026-07-08T00:00:00Z",
    }
    if agent_kind is not None:
        claim["agent_kind"] = agent_kind
    (bridges_dir / f"{sid}.json").write_text(json.dumps(claim), encoding="utf-8")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """One cwd whose ``.beacon/bridges/`` holds BOTH a claude-code and a codex
    claim, where the pid-tree matches BOTH (shared ancestor). Isolate ps /
    os.kill so only the two known bridge pids read as alive; no sibling-worktree
    scan (fast path only)."""
    cwd_bridges = tmp_path / "A" / ".beacon" / "bridges"

    monkeypatch.setattr(session, "_get_ancestor_pids", lambda: {MY_BCLAUDE_PID})
    monkeypatch.setattr(
        session, "_pid_alive",
        lambda pid: pid in {ALIVE_CLAUDE_BRIDGE, ALIVE_CODEX_BRIDGE},
    )
    monkeypatch.setattr(session, "_bridges_dir", lambda: cwd_bridges)
    monkeypatch.setattr(session, "_bridge_claim_path",
                        lambda: tmp_path / "A" / ".beacon" / "bridge.json")
    monkeypatch.setattr(session, "_in_linked_worktree", lambda: False)
    # Belt-and-suspenders: no real env leaking a caller kind into the test.
    monkeypatch.delenv("BEACON_BUS_SENDER", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    return cwd_bridges


def _seed_both_kinds(cwd_bridges: Path):
    _write_claim(cwd_bridges, "sv-CLAUDE", pid=ALIVE_CLAUDE_BRIDGE,
                 parent_pid=MY_BCLAUDE_PID, agent_kind="claude-code")
    _write_claim(cwd_bridges, "codex-1783398297606-65dbdb5e",
                 pid=ALIVE_CODEX_BRIDGE, parent_pid=MY_BCLAUDE_PID,
                 agent_kind="codex")


# -- AC #3: no cross-kind adoption when both claims match pid-tree -----------

def test_claude_caller_resolves_claude_sid_not_codex(wired, monkeypatch):
    """A Claude Code caller (CLAUDECODE=1) must resolve the claude-code sid even
    though the codex claim also matches the pid-tree."""
    _seed_both_kinds(wired)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert session.find_my_bridge_claim().get("session_id") == "sv-CLAUDE"


def test_codex_caller_resolves_codex_sid_not_claude(wired, monkeypatch):
    """A Codex caller (BEACON_BUS_SENDER=codex-…) must resolve the codex sid,
    never the co-located claude-code bridge."""
    _seed_both_kinds(wired)
    monkeypatch.setenv("BEACON_BUS_SENDER", "codex-1783398297606-65dbdb5e")
    assert (session.find_my_bridge_claim().get("session_id")
            == "codex-1783398297606-65dbdb5e")


def test_codex_sender_prefix_wins_over_claudecode(wired, monkeypatch):
    """If both signals are somehow present, the codex- sender prefix is the
    stronger marker (Codex daemon explicitly exports it)."""
    _seed_both_kinds(wired)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("BEACON_BUS_SENDER", "codex-1783398297606-65dbdb5e")
    assert (session.find_my_bridge_claim().get("session_id")
            == "codex-1783398297606-65dbdb5e")


def test_claude_caller_skips_lone_codex_claim(wired, monkeypatch):
    """When only a codex claim exists, a Claude Code caller adopts nothing —
    it does NOT masquerade as codex (this is the exact mis-attribution the bug
    produced in reverse)."""
    _write_claim(wired, "codex-XYZ", pid=ALIVE_CODEX_BRIDGE,
                 parent_pid=MY_BCLAUDE_PID, agent_kind="codex")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert session.find_my_bridge_claim() == {}


def test_codex_caller_skips_lone_claude_claim(wired, monkeypatch):
    """Symmetric: a codex caller adopts nothing when only a claude-code claim
    exists."""
    _write_claim(wired, "sv-ONLY", pid=ALIVE_CLAUDE_BRIDGE,
                 parent_pid=MY_BCLAUDE_PID, agent_kind="claude-code")
    monkeypatch.setenv("BEACON_BUS_SENDER", "codex-abc")
    assert session.find_my_bridge_claim() == {}


# -- AC #4: legacy claims (no agent_kind) stay adoptable by pid-tree alone ---

def test_legacy_claim_without_agent_kind_adopted_by_claude_caller(wired, monkeypatch):
    """A pre-e-3091 claim with no ``agent_kind`` field must still be adopted by
    a Claude Code caller — do not break existing single-bridge cwds."""
    _write_claim(wired, "sv-LEGACY", pid=ALIVE_CLAUDE_BRIDGE,
                 parent_pid=MY_BCLAUDE_PID, agent_kind=None)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert session.find_my_bridge_claim().get("session_id") == "sv-LEGACY"


def test_legacy_claim_without_agent_kind_adopted_by_codex_caller(wired, monkeypatch):
    """Same legacy claim is also adoptable by a codex caller — a claim with no
    kind matches anyone (pid-tree only)."""
    _write_claim(wired, "sv-LEGACY", pid=ALIVE_CLAUDE_BRIDGE,
                 parent_pid=MY_BCLAUDE_PID, agent_kind=None)
    monkeypatch.setenv("BEACON_BUS_SENDER", "codex-abc")
    assert session.find_my_bridge_claim().get("session_id") == "sv-LEGACY"


def test_unknown_caller_falls_back_to_pidtree_for_kinded_claim(wired, monkeypatch):
    """A caller we cannot classify (no env signal) is never blocked from its
    own bridge: a kinded claim is still adopted (degrades to pre-e-3091
    pid-tree-only behaviour)."""
    _write_claim(wired, "sv-SOLE", pid=ALIVE_CLAUDE_BRIDGE,
                 parent_pid=MY_BCLAUDE_PID, agent_kind="claude-code")
    # wired fixture already cleared both env signals → caller_kind == "".
    assert session.find_my_bridge_claim().get("session_id") == "sv-SOLE"


# -- pure helpers -----------------------------------------------------------

def test_caller_agent_kind_detects_codex_sender(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("BEACON_BUS_SENDER", "codex-123-abc")
    assert session._caller_agent_kind() == "codex"


def test_caller_agent_kind_detects_claude_code(monkeypatch):
    monkeypatch.delenv("BEACON_BUS_SENDER", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert session._caller_agent_kind() == "claude-code"


def test_caller_agent_kind_unknown_when_no_signal(monkeypatch):
    monkeypatch.delenv("BEACON_BUS_SENDER", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert session._caller_agent_kind() == ""


def test_caller_agent_kind_non_codex_sender_not_codex(monkeypatch):
    """A non-codex BEACON_BUS_SENDER (e.g. an sv- sid) must not classify as
    codex; it falls through to the CLAUDECODE check."""
    monkeypatch.setenv("BEACON_BUS_SENDER", "sv-999")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert session._caller_agent_kind() == "claude-code"


@pytest.mark.parametrize("claim_kind,caller_kind,expected", [
    ("", "", True),                       # legacy claim, unknown caller
    ("", "codex", True),                  # legacy claim, codex caller
    ("", "claude-code", True),            # legacy claim, claude caller
    ("codex", "codex", True),             # match
    ("claude-code", "claude-code", True), # match
    ("codex", "claude-code", False),      # cross-kind blocked
    ("claude-code", "codex", False),      # cross-kind blocked
    ("codex", "", True),                  # unknown caller matches anything
    ("claude-code", "", True),
])
def test_claim_kind_matches_caller_matrix(claim_kind, caller_kind, expected):
    assert session._claim_kind_matches_caller(claim_kind, caller_kind) is expected


# -- e-3091 (completing half): codex-session-pointer adoption is gated --------
#
# The REAL production trigger. At a Claude Code bus.mjs cold-start there is no
# bridge claim yet, so resolve_active_session_id() step 1 (read_bridge_session)
# returns empty and execution falls through to the codex-pointer adoption. Before
# the gate, a claude-code caller in a cwd with a co-located Codex daemon adopted
# the daemon's codex- sid (observed: a Claude Code session on codex-… with
# agent.kind=claude-code, so DMs to it vanished). These tests pin that a
# claude-code caller now SKIPS the pointer while codex / unknown callers still
# adopt it (preserving the e-2531 anti-churn benefit).

@pytest.fixture
def pointer_wired(monkeypatch):
    """Simulate cold-start: no bridge claim, a codex pointer present. Returns the
    codex sid the pointer publishes. The caller's kind is set per-test."""
    codex_sid = "codex-1783398297606-65dbdb5e"
    monkeypatch.setattr(session, "read_bridge_session", lambda: {})
    monkeypatch.setattr(session, "read_codex_session_pointer", lambda: codex_sid)
    # get_session_id() is the fall-through mint path; return a sentinel sv- so a
    # test can assert "fell through, did NOT adopt the codex sid".
    monkeypatch.setattr(session, "get_session_id", lambda: "sv-MINTED-FRESH")
    monkeypatch.delenv("BEACON_BUS_SENDER", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    return codex_sid


def test_claude_caller_does_not_adopt_codex_pointer(pointer_wired, monkeypatch):
    """The load-bearing fix: a claude-code caller must NOT adopt the codex
    pointer at cold-start — it falls through to mint its own sv-."""
    monkeypatch.setenv("CLAUDECODE", "1")
    assert session.resolve_active_session_id() == "sv-MINTED-FRESH"


def test_codex_caller_still_adopts_codex_pointer(pointer_wired, monkeypatch):
    """Anti-churn regression guard: a codex caller still adopts the stable
    codex- sid the daemon published (e-2531 behaviour preserved)."""
    codex_sid = pointer_wired
    monkeypatch.setenv("BEACON_BUS_SENDER", codex_sid)
    assert session.resolve_active_session_id() == codex_sid


def test_unknown_caller_still_adopts_codex_pointer(pointer_wired):
    """Unknown caller (no env signal) also adopts the pointer — the gate only
    excludes claude-code, so a Codex send path with a stripped env keeps its
    stable sender identity rather than churning."""
    codex_sid = pointer_wired
    # pointer_wired already cleared both env signals → caller_kind == "".
    assert session.resolve_active_session_id() == codex_sid


def test_bridge_claim_still_wins_over_pointer(monkeypatch):
    """Ordering unchanged: when a bridge claim DOES exist (warm bus.mjs), it is
    returned before the pointer is even consulted."""
    monkeypatch.setattr(session, "read_bridge_session",
                        lambda: {"session_id": "sv-WARM"})
    monkeypatch.setattr(session, "read_codex_session_pointer",
                        lambda: "codex-SHOULD-NOT-REACH")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert session.resolve_active_session_id() == "sv-WARM"
